"""LangGraph nodes. Each node returns a partial state update."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Mapping

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.config import get_stream_writer

from agent import prompts
from agent.narration import describe_end, describe_start
from agent.report import (
    TRANSCRIPT_ERROR,
    build_save_payload,
    compute_accuracy_score,
    enforce_evidence,
    render_error_report,
    render_report,
)
from agent.schemas import CriticOutput, ExtractionOutput, VerificationOutput
from agent.state import MAX_RETRIES, AgentState

INVESTIGATION_TOOLS = ("search_web_factcheck", "search_social_reactions", "execute_python_audit", "query_historical_claims")
MAX_TOOL_ROUNDS = 6
MAX_TRANSCRIPT_CHARS = 150_000
MAX_TOOL_RESULT_CHARS = 8_000
COMMENTS_FOR_PROMPT = 40


def emit(event: Dict[str, Any]) -> None:
    """Push a custom progress event to the stream consumer; no-op outside a graph run."""
    try:
        get_stream_writer()(event)
    except Exception:
        pass


def parse_tool_result(raw: Any) -> Any:
    """Normalize an MCP tool result (str / content blocks / (content, artifact)) into JSON data."""
    if isinstance(raw, tuple):
        raw = raw[0]
    if isinstance(raw, list):
        raw = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in raw)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {"raw": raw}
    return raw


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [truncated {len(text) - limit} chars]"


def _without_thinking(messages: List[BaseMessage]) -> List[BaseMessage]:
    """Drop thinking blocks (bound to the tool-loop request config) before the structured-output call."""
    cleaned: List[BaseMessage] = []
    for m in messages:
        if isinstance(m, AIMessage) and isinstance(m.content, list):
            text = "".join(b.get("text", "") for b in m.content if isinstance(b, dict) and b.get("type") == "text")
            m = AIMessage(content=text, tool_calls=m.tool_calls, id=m.id)
        cleaned.append(m)
    return cleaned


def _ai_text(ai: AIMessage) -> str:
    """Visible commentary the model wrote alongside its tool calls (thinking blocks excluded)."""
    if isinstance(ai.content, str):
        return ai.content.strip()
    return "".join(b.get("text", "") for b in ai.content if isinstance(b, dict) and b.get("type") == "text").strip()


class AgentNodes:
    def __init__(self, tools: Mapping[str, Any], llm: Any, max_tool_rounds: int = MAX_TOOL_ROUNDS) -> None:
        self.tools = tools
        self.llm = llm
        self.max_tool_rounds = max_tool_rounds

    async def _call(self, name: str, args: Dict[str, Any]) -> Any:
        call_id = uuid.uuid4().hex[:8]
        title, detail = describe_start(name, args)
        emit({"type": "tool_start", "id": call_id, "tool": name, "title": title, "detail": detail})
        try:
            if name not in self.tools:
                raise KeyError(f"tool {name!r} is not available")
            result = parse_tool_result(await self.tools[name].ainvoke(args))
        except Exception as exc:  # tool failures degrade the analysis instead of aborting it
            result = {"status": "ERROR", "message": f"{type(exc).__name__}: {exc}"}
        ok, summary = describe_end(name, result)
        emit({"type": "tool_end", "id": call_id, "tool": name, "ok": ok, "summary": summary})
        return result

    @asynccontextmanager
    async def _thinking(self, title: str, detail: str | None = None):
        """Timeline step for an LLM call, so the UI shows the agent working between tool calls."""
        step_id = uuid.uuid4().hex[:8]
        info = {"summary": "Done"}
        emit({"type": "tool_start", "id": step_id, "tool": "thinking", "title": title, "detail": detail})
        try:
            yield info
        except Exception:
            emit({"type": "tool_end", "id": step_id, "tool": "thinking", "ok": False, "summary": "Failed"})
            raise
        emit({"type": "tool_end", "id": step_id, "tool": "thinking", "ok": True, "summary": info["summary"]})

    # ---- Node 1 -------------------------------------------------------------------------------
    async def triage_node(self, state: AgentState) -> Dict[str, Any]:
        emit({"type": "node_start", "node": "triage"})
        meta = await self._call("get_video_metadata", {"video_url": state["video_url"]})
        if not isinstance(meta, dict) or meta.get("status") != "OK":
            message = (meta or {}).get("message", "unknown error") if isinstance(meta, dict) else str(meta)
            return {"error_message": f"Could not load video metadata: {message}", "retry_count": 0}
        history = await self._call("get_channel_history", {"channel_id": meta["channel_id"]})
        if not isinstance(history, dict) or history.get("status") == "ERROR":
            history = {}
        emit({"type": "video", "title": meta["title"], "channel": meta["channel_name"], "upload_date": meta["upload_date"][:10]})
        return {"video_id": meta["video_id"], "metadata": meta, "channel_history": history, "retry_count": 0}

    # ---- Node 2 -------------------------------------------------------------------------------
    async def extraction_node(self, state: AgentState) -> Dict[str, Any]:
        emit({"type": "node_start", "node": "extraction"})
        video_id, meta = state["video_id"], state["metadata"]
        transcript = await self._call("get_transcript", {"video_id": video_id})
        if not isinstance(transcript, dict) or transcript.get("status") != "OK":
            if isinstance(transcript, dict) and transcript.get("status") not in (None, "TRANSCRIPTS_DISABLED"):
                return {"error_message": transcript.get("message", TRANSCRIPT_ERROR)}
            return {"error_message": TRANSCRIPT_ERROR}
        comments = await self._call(
            "get_comments", {"video_id": video_id, "max_results": 60, "sort_by": "top", "channel_id": meta["channel_id"]}
        )

        transcript_text = _clip(
            "\n".join(f"[{s['timestamp']}] {s['text']}" for s in transcript["segments"]), MAX_TRANSCRIPT_CHARS
        )
        comment_lines = []
        if isinstance(comments, dict):
            for c in comments.get("comments", [])[:COMMENTS_FOR_PROMPT]:
                creator = " (CREATOR)" if c.get("is_creator") else ""
                comment_lines.append(f"- {c['author']}{creator} [{c['likes']} likes]: {_clip(c['text'], 500)}")
        history = state.get("channel_history", {})
        brief = {
            "title": meta["title"],
            "channel": meta["channel_name"],
            "upload_date": meta["upload_date"],
            "description": _clip(meta["description"], 3000),
            "links": meta["links"],
            "promo_codes": meta["promo_codes"],
            "chapters": meta["chapters"],
            "channel_history": {k: history.get(k) for k in ("total_videos_analyzed", "avg_accuracy_score", "frequent_sponsors")},
        }
        human = (
            f"VIDEO METADATA:\n{json.dumps(brief, ensure_ascii=False)}\n\n"
            f"TRANSCRIPT (language={transcript['language']}):\n{transcript_text}\n\n"
            f"TOP VIEWER COMMENTS:\n{chr(10).join(comment_lines) or '(none available)'}"
        )
        async with self._thinking(
            "Reading the transcript for clickbait, sponsors and checkable claims", "this can take a few seconds"
        ) as step:
            out: ExtractionOutput = await self.llm.with_structured_output(ExtractionOutput).ainvoke(
                [SystemMessage(content=prompts.EXTRACTION_SYSTEM), HumanMessage(content=human)]
            )
            step["summary"] = f"Found {len(out.claims)} claim{'s' if len(out.claims) != 1 else ''} to verify"
        summary = out.model_dump()
        claims = summary.pop("claims")
        emit(
            {
                "type": "overview",
                "domain": out.domain,
                "clickbait": summary["clickbait"],
                "sponsors": [s["name"] for s in summary["sponsors"]],
                "has_paid_promotion": out.has_paid_promotion,
                "bias_notes": out.bias_notes,
            }
        )
        emit({"type": "claims", "claims": claims})
        return {
            "transcript_summary": summary,
            "extracted_claims": claims,
            "metadata": {**meta, "domain": out.domain},
        }

    # ---- Node 3 -------------------------------------------------------------------------------
    async def investigator_node(self, state: AgentState) -> Dict[str, Any]:
        emit({"type": "node_start", "node": "investigator", "attempt": state.get("retry_count", 0)})
        retry = state.get("retry_count", 0)
        feedback = state.get("critic_feedback") or []
        summary = state["transcript_summary"]

        if feedback:
            task = (
                "Re-investigate ONLY these weak claims using the suggested queries (search_depth='advanced'):\n"
                + json.dumps(feedback, ensure_ascii=False)
            )
        else:
            task = "Investigate these claims:\n" + json.dumps(state["extracted_claims"], ensure_ascii=False)
        human = (
            f"Video: {state['metadata']['title']} (domain: {state['metadata'].get('domain', 'unknown')})\n"
            f"Topic tags: {summary.get('topic_tags', [])}\n\n{task}"
        )
        messages: List[BaseMessage] = [SystemMessage(content=prompts.INVESTIGATOR_SYSTEM), HumanMessage(content=human)]
        for c in feedback or state["extracted_claims"]:
            emit({"type": "note", "text": f"Investigating: “{c['claim']}”" + (f" — new angle: {c['suggested_query']}" if feedback else "")})

        bound = self.llm.bind_tools([self.tools[n] for n in INVESTIGATION_TOOLS if n in self.tools])
        for _ in range(self.max_tool_rounds):
            ai: AIMessage = await bound.ainvoke(messages)
            messages.append(ai)
            if ai.tool_calls and (commentary := _ai_text(ai)):
                emit({"type": "note", "text": commentary})
            if not ai.tool_calls:
                break
            messages.extend(await asyncio.gather(*(self._run_tool_call(tc, retry) for tc in ai.tool_calls)))
        if isinstance(messages[-1], AIMessage) and messages[-1].tool_calls:  # unreachable guard
            messages.pop()

        messages.append(HumanMessage(content=prompts.VERIFICATION_FINAL))
        async with self._thinking("Weighing the evidence and writing verdicts") as step:
            out: VerificationOutput = await self.llm.with_structured_output(VerificationOutput).ainvoke(
                _without_thinking(messages)
            )
            step["summary"] = f"{len(out.results)} verdict{'s' if len(out.results) != 1 else ''} drafted"

        merged = {r["claim"]: r for r in state.get("verification_results", [])}
        for r in out.results:
            merged[r.claim] = enforce_evidence(r.model_dump())
        results = []
        for c in state["extracted_claims"]:  # every extracted claim gets a verdict, even if the LLM skipped it
            results.append(
                merged.get(c["claim"])
                or {
                    "timestamp": c["timestamp"],
                    "claim": c["claim"],
                    "verdict": "Unverified",
                    "evidence": "Lacks evidence: the claim was not investigated.",
                    "sources": [],
                    "confidence": "low",
                }
            )

        for r in results:
            emit({"type": "verdict", "result": r})

        seen = {a["detail"] for a in state.get("community_insights", [])}
        insights = list(state.get("community_insights", [])) + [
            a.model_dump() for a in out.community_insights if a.detail not in seen
        ]
        return {"verification_results": results, "community_insights": insights}

    async def _run_tool_call(self, tool_call: Dict[str, Any], retry: int) -> ToolMessage:
        args = dict(tool_call["args"])
        if tool_call["name"] == "search_web_factcheck" and retry > 0:
            args["search_depth"] = "advanced"
        result = await self._call(tool_call["name"], args)
        return ToolMessage(
            content=_clip(json.dumps(result, ensure_ascii=False), MAX_TOOL_RESULT_CHARS), tool_call_id=tool_call["id"]
        )

    # ---- Node 4 -------------------------------------------------------------------------------
    async def critic_node(self, state: AgentState) -> Dict[str, Any]:
        emit({"type": "node_start", "node": "critic"})
        retry = state.get("retry_count", 0)
        weak = [r for r in state.get("verification_results", []) if r["verdict"] == "Unverified" or r["confidence"] == "low"]
        if not weak or retry >= MAX_RETRIES:
            return {"critic_feedback": []}

        by_claim = {r["claim"]: r for r in weak}
        emit(
            {
                "type": "note",
                "text": f"{len(weak)} claim{'s have' if len(weak) != 1 else ' has'} thin evidence. "
                f"Retrying with sharper searches (attempt {retry + 1} of {MAX_RETRIES}).",
            }
        )
        try:
            async with self._thinking("Reviewing weak results and rewriting the searches"):
                out: CriticOutput = await self.llm.with_structured_output(CriticOutput).ainvoke(
                    [
                        SystemMessage(content=prompts.CRITIC_SYSTEM),
                        HumanMessage(content=json.dumps([{"claim": r["claim"], "evidence": r["evidence"]} for r in weak], ensure_ascii=False)),
                    ]
                )
            queries = {w.claim: w.suggested_query for w in out.weak_claims}
        except Exception:
            queries = {}
        feedback = [
            {"claim": c, "timestamp": r["timestamp"], "suggested_query": queries.get(c, c)} for c, r in by_claim.items()
        ]
        return {"critic_feedback": feedback, "retry_count": retry + 1}

    # ---- Node 5 -------------------------------------------------------------------------------
    async def commit_and_report_node(self, state: AgentState) -> Dict[str, Any]:
        emit({"type": "node_start", "node": "report"})
        if state.get("error_message"):
            return {"final_report_md": render_error_report(state["error_message"], state), "saved_to_memory": False}

        results = state.get("verification_results", [])
        score = compute_accuracy_score(results)
        report = render_report(state, results, score)
        saved = await self._call("save_video_analysis", {"payload_json": json.dumps(build_save_payload(state, results, score))})
        ok = isinstance(saved, dict) and saved.get("status") == "success"
        if not ok:
            message = saved.get("message", "unknown error") if isinstance(saved, dict) else str(saved)
            report += f"\n> ⚠️ This analysis could not be saved to channel history: {message}\n"
        return {"final_report_md": report, "overall_accuracy_score": score, "saved_to_memory": ok, "verification_results": results}

"""LangGraph nodes. Each node returns a partial state update."""

from __future__ import annotations

import asyncio
import json
import re
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Mapping

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.exceptions import OutputParserException
from langgraph.config import get_stream_writer
from pydantic import ValidationError

from agent import prompts
from agent.narration import describe_end, describe_start
from agent.runlog import clip, clip_obj, log_event, log_prompts_enabled
from agent.report import (
    TRANSCRIPT_ERROR,
    build_report,
    build_save_payload,
    clean_outside_discussion,
    compute_accuracy_score,
    enforce_evidence,
    render_error_report,
    render_markdown,
)
from agent.schemas import CommentsAnalysis, CriticOutput, SynthesisOutput, VerificationOutput, VideoAnalysis
from agent.state import MAX_RETRIES, AgentState

INVESTIGATION_TOOLS = ("search_web_factcheck", "search_social_reactions", "execute_python_audit", "query_historical_claims")
MAX_TOOL_ROUNDS = 6
MAX_TRANSCRIPT_CHARS = 150_000
MAX_TOOL_RESULT_CHARS = 8_000
COMMENTS_FETCHED = 100  # top-level comments requested (one API page); their replies come with them
REPLIES_PER_COMMENT = 3  # most-liked replies shown to the model per comment
MAX_COMMENT_CHARS = 60_000  # comments arrive best-first, so the least relevant ones are what gets cut


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


def format_comments(comments: List[Dict[str, Any]]) -> List[str]:
    """One text block per comment thread: the comment, then its most-liked replies indented beneath it."""

    def line(c: Dict[str, Any], limit: int) -> str:
        creator = " (CREATOR)" if c.get("is_creator") else ""
        return f"{c['author']}{creator} [{c['likes']} likes]: {_clip(' '.join(c['text'].split()), limit)}"

    blocks, used = [], 0
    for c in comments:
        replies = sorted(c.get("replies") or [], key=lambda r: r.get("likes", 0), reverse=True)[:REPLIES_PER_COMMENT]
        block = "\n".join([f"- {line(c, 400)}"] + [f"  > {line(r, 250)}" for r in replies])
        if used + len(block) > MAX_COMMENT_CHARS:
            break
        blocks.append(block)
        used += len(block)
    return blocks


def topic_cap(duration_sec: int) -> int:
    """How many topic paragraphs to write: about one per 3 minutes of video, 3 to 10."""
    return max(3, min(10, 2 + int(duration_sec // 180)))


def claim_cap(segments: List[Dict[str, Any]]) -> int:
    """How many claims to fact-check: scales with video length (about one per 4 minutes), 3 to 8."""
    minutes = (segments[-1].get("start_sec", 0) / 60) if segments else 0
    return max(3, min(8, 2 + int(minutes // 4)))


ABSENCE = re.compile(r"^(the video |this video )?(contains |has |includes |there (is|are) )?(no|none|n/a|nothing)\b", re.IGNORECASE)


def drop_empty_sections(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove dynamic sections that carry nothing: no items, or only statements that something is absent."""
    kept = []
    for sec in sections:
        items = [it for it in sec["items"] if it["text"].strip() and not ABSENCE.match(it["text"].strip())]
        if items:
            kept.append({**sec, "items": items})
    return kept


class AgentNodes:
    def __init__(self, tools: Mapping[str, Any], llm: Any, max_tool_rounds: int = MAX_TOOL_ROUNDS) -> None:
        self.tools = tools
        self.llm = llm
        self.max_tool_rounds = max_tool_rounds

    async def _call(self, name: str, args: Dict[str, Any]) -> Any:
        call_id = uuid.uuid4().hex[:8]
        title, detail = describe_start(name, args)
        log_event("tool_call_start", id=call_id, tool=name, args=clip_obj(args, 1500))  # logged first so the trace shows it live
        emit({"type": "tool_start", "id": call_id, "tool": name, "title": title, "detail": detail})
        started, error = time.monotonic(), None
        try:
            if name not in self.tools:
                raise KeyError(f"tool {name!r} is not available")
            result = parse_tool_result(await self.tools[name].ainvoke(args))
        except Exception as exc:  # tool failures degrade the analysis instead of aborting it
            result = {"status": "ERROR", "message": f"{type(exc).__name__}: {exc}"}
            error = traceback.format_exc()
        ok, summary = describe_end(name, result)
        log_event(
            "tool_call", id=call_id, tool=name, args=clip_obj(args, 1500), ok=ok, summary=summary,
            duration_ms=round((time.monotonic() - started) * 1000), result=clip(result), error=error,
        )
        emit({"type": "tool_end", "id": call_id, "tool": name, "ok": ok, "summary": summary})
        return result

    async def _structured(self, schema: Any, messages: List[BaseMessage]) -> Any:
        """Structured LLM call that retries once, feeding the validation error back, when the model skips a field."""
        runnable = self.llm.with_structured_output(schema)
        try:
            return await runnable.ainvoke(messages)
        except (ValidationError, OutputParserException) as exc:
            log_event("structured_retry", schema=schema.__name__, error=clip(str(exc), 800))
            fix = HumanMessage(
                content="Your previous answer failed validation:\n" + clip(str(exc), 600)
                + "\nReturn the COMPLETE object again, with every required field."
            )
            return await runnable.ainvoke(list(messages) + [fix])

    def _model_name(self) -> str:
        return str(getattr(self.llm, "model", None) or getattr(self.llm, "model_name", None) or type(self.llm).__name__)

    @asynccontextmanager
    async def _thinking(self, title: str, detail: str | None = None, input_chars: int | None = None):
        """Timeline step for an LLM call, so the UI shows the agent working between tool calls."""
        step_id = uuid.uuid4().hex[:8]
        info: Dict[str, Any] = {"summary": "Done"}
        started = time.monotonic()
        log_event("llm_call_start", id=step_id, purpose=title, model=self._model_name(), input_chars=input_chars)
        emit({"type": "tool_start", "id": step_id, "tool": "thinking", "title": title, "detail": detail})
        try:
            yield info
        except Exception:
            emit({"type": "tool_end", "id": step_id, "tool": "thinking", "ok": False, "summary": "Failed"})
            log_event(
                "llm_call", id=step_id, purpose=title, ok=False,
                duration_ms=round((time.monotonic() - started) * 1000), error=traceback.format_exc(),
            )
            raise
        emit({"type": "tool_end", "id": step_id, "tool": "thinking", "ok": True, "summary": info["summary"]})
        log_event(
            "llm_call", id=step_id, purpose=title, ok=True, duration_ms=round((time.monotonic() - started) * 1000),
            summary=info["summary"], prompt_chars=info.get("prompt_chars"), output=clip(info.get("output", "")),
            prompt=info.get("prompt") if log_prompts_enabled() else None,
        )

    # ---- Node 1 -------------------------------------------------------------------------------
    async def triage_node(self, state: AgentState) -> Dict[str, Any]:
        emit({"type": "node_start", "node": "triage"})
        meta = await self._call("get_video_metadata", {"video_url": state["video_url"]})
        if not isinstance(meta, dict) or meta.get("status") != "OK":
            message = (meta or {}).get("message", "unknown error") if isinstance(meta, dict) else str(meta)
            return {"error_message": f"Could not load video metadata: {message}", "retry_count": 0}
        history = await self._call("get_channel_history", {"channel_id": meta["channel_id"]})
        if not isinstance(history, dict) or history.get("status") == "ERROR" or "raw" in history:
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
            "get_comments", {"video_id": video_id, "max_results": COMMENTS_FETCHED, "sort_by": "top", "channel_id": meta["channel_id"]}
        )

        transcript_text = _clip(
            "\n".join(f"[{s['timestamp']}] {s['text']}" for s in transcript["segments"]), MAX_TRANSCRIPT_CHARS
        )
        comment_lines = format_comments(comments.get("comments", [])) if isinstance(comments, dict) else []
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
        cap = claim_cap(transcript["segments"])
        human = (
            f"VIDEO METADATA:\n{json.dumps(brief, ensure_ascii=False)}\n\n"
            f"Return at most {cap} claims.\n\n"
            f"TRANSCRIPT (language={transcript['language']}):\n{transcript_text}"
        )

        analysis, comment_out = await asyncio.gather(
            self._analyze_video(human), self._analyze_comments(meta["title"], comment_lines)
        )

        summary = analysis.model_dump()
        if not summary["topic_tags"]:  # the model skipped them: derive from the key ideas so memory lookups still work
            summary["topic_tags"] = list(dict.fromkeys(kp["concept"].lower() for kp in summary["key_points"]))[:3]
        summary["extra_sections"] = drop_empty_sections(summary["extra_sections"])
        summary["bias_notes"] = "" if ABSENCE.match(summary["bias_notes"].strip()) else summary["bias_notes"]
        claims = summary.pop("claims")[:cap]
        emit({"type": "summary", "video_type": analysis.video_type, "tldr": analysis.tldr, "key_points": summary["key_points"]})
        emit(
            {
                "type": "overview",
                "domain": analysis.video_type,
                "clickbait": summary["clickbait"],
                "sponsors": [s["name"] for s in summary["sponsors"]],
                "has_paid_promotion": analysis.has_paid_promotion,
                "bias_notes": analysis.bias_notes,
            }
        )
        emit({"type": "claims", "claims": claims})
        insights = [c.model_dump() for c in comment_out.insights if c.adds_information] if comment_out else []
        if insights:
            emit({"type": "comments", "insights": insights})
        return {
            "transcript_summary": summary,
            "transcript_text": transcript_text,
            "extracted_claims": claims,
            "comment_insights": insights,
            "metadata": {**meta, "video_type": analysis.video_type},
        }

    async def _analyze_video(self, human: str) -> VideoAnalysis:
        async with self._thinking(
            "Summarizing the video and picking out claims worth checking", "this can take a few seconds", input_chars=len(human)
        ) as step:
            step["prompt_chars"], step["prompt"] = len(human), human
            out: VideoAnalysis = await self._structured(VideoAnalysis, 
                [SystemMessage(content=prompts.ANALYSIS_SYSTEM), HumanMessage(content=human)]
            )
            step["output"] = out.model_dump_json()
            step["summary"] = f"{len(out.key_points)} key points, {len(out.claims)} claim{'s' if len(out.claims) != 1 else ''} to verify"
        return out

    async def _analyze_comments(self, title: str, comment_lines: List[str]) -> CommentsAnalysis | None:
        if not comment_lines:
            return None
        human = f"VIDEO TITLE: {title}\n\nTOP VIEWER COMMENTS:\n" + "\n".join(comment_lines)
        try:
            async with self._thinking("Reading the comments for corrections, fixes and extra context", input_chars=len(human)) as step:
                step["prompt_chars"], step["prompt"] = len(human), human
                out: CommentsAnalysis = await self._structured(CommentsAnalysis, 
                    [SystemMessage(content=prompts.COMMENTS_SYSTEM), HumanMessage(content=human)]
                )
                step["output"] = out.model_dump_json()
                n = sum(c.adds_information for c in out.insights)
                step["summary"] = f"{n} comment{'s' if n != 1 else ''} added information" if n else "No comments added information"
            return out
        except Exception:  # the comments are a bonus; never fail the analysis over them
            return None

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
            f"Video: {state['metadata']['title']} (type: {state['metadata'].get('video_type', 'unknown')})\n"
            f"Topic tags: {summary.get('topic_tags', [])}\n\n{task}"
        )
        messages: List[BaseMessage] = [SystemMessage(content=prompts.INVESTIGATOR_SYSTEM), HumanMessage(content=human)]
        for c in feedback or state["extracted_claims"]:
            emit({"type": "note", "text": f"Investigating: “{c['claim']}”" + (f" — new angle: {c['suggested_query']}" if feedback else "")})

        bound = self.llm.bind_tools([self.tools[n] for n in INVESTIGATION_TOOLS if n in self.tools])
        for round_no in range(self.max_tool_rounds):
            log_event("llm_turn_start", round=round_no, model=self._model_name(), messages=len(messages))
            ai: AIMessage = await bound.ainvoke(messages)
            messages.append(ai)
            log_event(
                "llm_turn", round=round_no, text=clip(_ai_text(ai), 1000),
                tool_calls=[{"name": tc["name"], "args": clip(tc["args"], 500)} for tc in ai.tool_calls],
            )
            if ai.tool_calls and (commentary := _ai_text(ai)):
                emit({"type": "note", "text": commentary})
            if not ai.tool_calls:
                break
            messages.extend(await asyncio.gather(*(self._run_tool_call(tc, retry) for tc in ai.tool_calls)))
        if isinstance(messages[-1], AIMessage) and messages[-1].tool_calls:  # unreachable guard
            messages.pop()

        messages.append(HumanMessage(content=prompts.VERIFICATION_FINAL))
        async with self._thinking(
            "Weighing the evidence and writing verdicts", input_chars=sum(len(str(m.content)) for m in messages)
        ) as step:
            out: VerificationOutput = await self._structured(VerificationOutput, 
                _without_thinking(messages)
            )
            step["output"] = out.model_dump_json()
            step["summary"] = f"{len(out.results)} verdict{'s' if len(out.results) != 1 else ''} drafted"

        merged = {r["claim"]: r for r in state.get("verification_results", [])}
        for r in out.results:
            merged[r.claim] = enforce_evidence(r.model_dump())
        results = []
        for c in state["extracted_claims"]:  # every extracted claim gets a verdict, even if the LLM skipped it
            result = merged.get(c["claim"]) or {
                "timestamp": c["timestamp"],
                "claim": c["claim"],
                "verdict": "Unverified",
                "evidence": "Lacks evidence: the claim was not investigated.",
                "sources": [],
                "confidence": "low",
            }
            results.append({**result, "importance": c.get("importance", "medium")})

        for r in results:
            emit({"type": "verdict", "result": r})
            log_event("verdict", claim=r["claim"], verdict=r["verdict"], confidence=r["confidence"], sources=r["sources"])

        seen = {a["detail"] for a in state.get("community_insights", [])}
        outside = list(state.get("community_insights", [])) + [
            a.model_dump() for a in out.outside_discussion if a.detail not in seen
        ]
        return {"verification_results": results, "community_insights": outside}

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
        weak = [
            r for r in state.get("verification_results", [])
            if (r["verdict"] == "Unverified" or r["confidence"] == "low") and r.get("importance", "medium") != "low"
        ]  # a low-importance passing remark is not worth another round of searches
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
            async with self._thinking("Reviewing weak results and rewriting the searches") as step:
                out: CriticOutput = await self._structured(CriticOutput, 
                    [
                        SystemMessage(content=prompts.CRITIC_SYSTEM),
                        HumanMessage(content=json.dumps([{"claim": r["claim"], "evidence": r["evidence"]} for r in weak], ensure_ascii=False)),
                    ]
                )
            step["output"] = out.model_dump_json()
            queries = {w.claim: w.suggested_query for w in out.weak_claims}
            for w in out.weak_claims:
                if w.reason:
                    emit({"type": "note", "text": f"Rethinking “{w.claim}”: {w.reason} New query: {w.suggested_query}"})
        except Exception:
            queries = {}
        feedback = [
            {"claim": c, "timestamp": r["timestamp"], "suggested_query": queries.get(c, c)} for c, r in by_claim.items()
        ]
        return {"critic_feedback": feedback, "retry_count": retry + 1}

    # ---- Node 5 -------------------------------------------------------------------------------
    async def synthesis_node(self, state: AgentState) -> Dict[str, Any]:
        """Write the topic-by-topic summary from the transcript, the fact-check evidence, web discussion and comments."""
        emit({"type": "node_start", "node": "synthesis"})
        summary = state.get("transcript_summary", {})
        results = state.get("verification_results", [])
        outside = clean_outside_discussion(state.get("community_insights", []))
        cap = topic_cap(state.get("metadata", {}).get("duration_sec", 0) or 0)
        evidence = [{k: r.get(k) for k in ("timestamp", "claim", "verdict", "evidence", "sources")} for r in results]
        comments = [{k: c[k] for k in ("author", "likes", "category", "detail")} for c in state.get("comment_insights", [])]
        web = [{k: o[k] for k in ("source", "url", "category", "detail")} for o in outside]
        sections = [
            f"VIDEO: {state['metadata']['title']} (type: {summary.get('video_type', 'other')})\n"
            f"Short summary: {summary.get('tldr', '')}\n\nWrite between {max(3, cap - 2)} and {cap} topics.",
            f"FACT-CHECK RESULTS:\n{json.dumps(evidence, ensure_ascii=False)}",
        ]
        if web:  # empty sections are left out entirely, or the model writes sentences about their absence
            sections.append(f"WEB DISCUSSION:\n{json.dumps(web, ensure_ascii=False)}")
        if comments:
            sections.append(f"USEFUL POINTS FROM VIEWER COMMENTS AND REPLIES:\n{json.dumps(comments, ensure_ascii=False)}")
        sections.append(f"TRANSCRIPT:\n{state.get('transcript_text', '')}")
        human = "\n\n".join(sections)
        allowed = {u for r in results for u in r.get("sources", [])} | {o["url"] for o in outside}
        topics: List[Dict[str, Any]] = []
        overview = ""
        try:
            async with self._thinking(
                "Writing the topic-by-topic summary", "combining the video, the web evidence and the comments", input_chars=len(human)
            ) as step:
                step["prompt_chars"], step["prompt"] = len(human), human
                out: SynthesisOutput = await self._structured(
                    SynthesisOutput, [SystemMessage(content=prompts.SYNTHESIS_SYSTEM), HumanMessage(content=human)]
                )
                step["output"] = out.model_dump_json()
                overview = out.overview.strip()
                topics = [
                    {**t.model_dump(), "source_urls": [u for u in t.source_urls if u in allowed]}
                    for t in out.topics
                    if t.summary.strip() and t.title.strip()
                ]
                step["summary"] = f"{len(topics)} topic{'s' if len(topics) != 1 else ''}"
        except Exception:
            topics = []
        if not topics:  # the model failed: fall back to the key ideas so the report still has a body
            emit({"type": "note", "text": "The topic summary could not be written, so the key ideas are shown instead."})
            topics = [
                {"title": kp["concept"], "timestamp": kp["timestamp"], "summary": kp["point"], "source_urls": []}
                for kp in summary.get("key_points", [])
            ]
        return {"topics": topics, "overview": overview}

    # ---- Node 6 -------------------------------------------------------------------------------
    async def commit_and_report_node(self, state: AgentState) -> Dict[str, Any]:
        emit({"type": "node_start", "node": "report"})
        if state.get("error_message"):
            return {"final_report_md": render_error_report(state["error_message"], state), "saved_to_memory": False}

        results = state.get("verification_results", [])
        score = compute_accuracy_score(results)
        report = build_report(state, results, score)
        markdown = render_markdown(report)
        saved = await self._call(
            "save_video_analysis", {"payload_json": json.dumps(build_save_payload(state, results, score, report))}
        )
        ok = isinstance(saved, dict) and saved.get("status") == "success"
        if not ok:
            message = saved.get("message", "unknown error") if isinstance(saved, dict) else str(saved)
            markdown += f"\n> ⚠️ This analysis could not be saved to channel history: {message}\n"
        return {
            "report": report, "final_report_md": markdown, "overall_accuracy_score": score,
            "saved_to_memory": ok, "verification_results": results,
        }

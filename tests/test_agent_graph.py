"""Graph tests with fake tools and a scripted fake LLM (no network)."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
from langchain_core.messages import AIMessage

from agent.graph import build_graph, stream_graph
from agent.nodes import parse_tool_result
from agent.report import compute_accuracy_score, enforce_evidence, timestamp_to_seconds
from agent.schemas import (
    ClaimVerdict,
    ClickbaitBuster,
    Claim,
    CriticOutput,
    ExtractionOutput,
    SponsorMention,
    VerificationOutput,
    WeakClaim,
)

META = {
    "status": "OK",
    "video_id": "dQw4w9WgXcQ",
    "title": "Is Library X really 4x faster?",
    "channel_id": "UC1",
    "channel_name": "Chan",
    "upload_date": "2025-01-01T00:00:00Z",
    "description": "d",
    "links": [{"url": "https://amzn.to/x", "domain": "amzn.to", "affiliate": True, "shortener": True, "utm": False}],
    "promo_codes": ["ABC12"],
    "chapters": [],
}


class FakeTool:
    def __init__(self, name: str, result: Any, calls: List):
        self.name, self._result, self._calls = name, result, calls

    async def ainvoke(self, args: Dict[str, Any]):
        self._calls.append((self.name, args))
        if isinstance(self._result, Exception):
            raise self._result
        return json.dumps(self._result) if not isinstance(self._result, str) else self._result


def make_tools(calls: List, overrides: Dict[str, Any] | None = None) -> Dict[str, FakeTool]:
    results = {
        "get_video_metadata": META,
        "get_channel_history": {"channel_id": "UC1", "total_videos_analyzed": 2, "avg_accuracy_score": 70.0, "frequent_sponsors": ["NordVPN"]},
        "get_transcript": {"status": "OK", "language": "en", "segments": [{"timestamp": "04:32", "start_sec": 272, "text": "4x faster"}]},
        "get_comments": {"status": "OK", "comments": [{"author": "@dev", "text": "use --legacy-peer-deps", "likes": 5, "is_creator": False}]},
        "search_web_factcheck": {"results": [{"url": "https://docs.x/bench"}]},
        "search_social_reactions": {"results": []},
        "execute_python_audit": {"stdout": "1"},
        "query_historical_claims": [],
        "save_video_analysis": {"status": "success"},
    }
    results.update(overrides or {})
    return {n: FakeTool(n, r, calls) for n, r in results.items()}


EXTRACTION = ExtractionOutput(
    clickbait=ClickbaitBuster(verdict="Only about 1.4x.", payoff_timestamp="08:14", clickbait_rating="Medium"),
    claims=[Claim(timestamp="04:32", claim="Library X is 4x faster than Y.", quantitative=False)],
    sponsors=[SponsorMention(name="NordVPN", timestamp_range="02:10 - 03:45", disclosed=False)],
    has_paid_promotion=True,
    disclosed_properly=False,
    bias_notes="Praises X without noting affiliate link.",
    topic_tags=["python"],
    domain="Tech Tutorial",
)


def verdict(v="Partially False", sources=("https://docs.x/bench",), conf="high") -> ClaimVerdict:
    return ClaimVerdict(
        timestamp="04:32", claim="Library X is 4x faster than Y.", verdict=v, evidence="1.4x average.", sources=list(sources), confidence=conf
    )


class FakeLLM:
    """Scripted responses: structured outputs keyed by schema, tool-loop AIMessages queued in order."""

    def __init__(self, structured: Dict[type, List[Any]], tool_turns: List[AIMessage] | None = None):
        self.structured = {k: list(v) for k, v in structured.items()}
        self.tool_turns = list(tool_turns or [AIMessage(content="done")])
        self.structured_calls: List[type] = []

    def bind_tools(self, tools):
        return self

    def with_structured_output(self, schema):
        llm = self

        class _S:
            async def ainvoke(self, messages):
                llm.structured_calls.append(schema)
                queue = llm.structured[schema]
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(item, Exception):
                    raise item
                return item

        return _S()

    async def ainvoke(self, messages):
        return self.tool_turns.pop(0) if len(self.tool_turns) > 1 else self.tool_turns[0]


async def run(graph, url="https://youtu.be/dQw4w9WgXcQ"):
    events = [e async for e in stream_graph(graph, url)]
    return events, events[-1]["state"]


async def test_happy_path_scores_and_commits():
    calls: List = []
    llm = FakeLLM(
        {ExtractionOutput: [EXTRACTION], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]},
        tool_turns=[
            AIMessage(content="", tool_calls=[{"name": "search_web_factcheck", "args": {"query": "x"}, "id": "1"}]),
            AIMessage(content="done"),
        ],
    )
    events, state = await run(build_graph(make_tools(calls), llm))

    assert state["overall_accuracy_score"] == 35
    assert state["saved_to_memory"] is True
    assert "Fact-Check Scorecard (Overall Accuracy: 35/100)" in state["final_report_md"]
    assert "Recurring sponsor" in state["final_report_md"]
    saved = json.loads(next(a for n, a in calls if n == "save_video_analysis")["payload_json"])
    assert saved["channel_id"] == "UC1" and saved["overall_accuracy_score"] == 35
    assert saved["sponsors"] == ["NordVPN"] and saved["analysis_payload"]["claims_verified"][0]["verdict"] == "Partially False"
    assert [e["node"] for e in events if e["type"] == "node_end"] == ["triage", "extraction", "investigator", "critic", "commit_and_report"]
    assert any(e["type"] == "tool_start" and e["tool"] == "search_web_factcheck" for e in events)


async def test_missing_transcript_aborts_without_commit():
    calls: List = []
    tools = make_tools(calls, {"get_transcript": {"status": "TRANSCRIPTS_DISABLED", "segments": [], "message": "x"}})
    llm = FakeLLM({})
    events, state = await run(build_graph(tools, llm))

    assert "Transcripts are disabled or unavailable for this video; cannot perform spoken claim auditing" in state["final_report_md"]
    assert not any(n == "save_video_analysis" for n, _ in calls)
    assert llm.structured_calls == []
    assert state["saved_to_memory"] is False


async def test_metadata_failure_aborts_cleanly():
    calls: List = []
    tools = make_tools(calls, {"get_video_metadata": {"status": "ERROR", "message": "bad url"}})
    _, state = await run(build_graph(tools, FakeLLM({})))
    assert "bad url" in state["final_report_md"]
    assert not any(n == "save_video_analysis" for n, _ in calls)


async def test_critic_retries_at_most_twice_then_reports_best_effort():
    calls: List = []
    weak = verdict("Unverified", sources=(), conf="low")
    llm = FakeLLM(
        {
            ExtractionOutput: [EXTRACTION],
            VerificationOutput: [VerificationOutput(results=[weak])],
            CriticOutput: [CriticOutput(weak_claims=[WeakClaim(claim=weak.claim, suggested_query="library x benchmark official")])],
        },
        tool_turns=[
            AIMessage(content="", tool_calls=[{"name": "search_web_factcheck", "args": {"query": "q", "search_depth": "basic"}, "id": "1"}]),
            AIMessage(content="done"),
        ],
    )
    _, state = await run(build_graph(make_tools(calls), llm))

    assert state["retry_count"] == 2
    investigations = [c for c in llm.structured_calls if c is VerificationOutput]
    assert len(investigations) == 3  # initial + 2 retries
    searches = [a for n, a in calls if n == "search_web_factcheck"]
    assert searches[0]["search_depth"] == "basic" or len(searches) >= 1
    assert all(a["search_depth"] == "advanced" for a in searches[1:])
    assert "Unverified" in state["final_report_md"]
    assert state["saved_to_memory"] is True


async def test_verdict_without_sources_is_downgraded_to_unverified():
    llm = FakeLLM(
        {
            ExtractionOutput: [EXTRACTION],
            VerificationOutput: [VerificationOutput(results=[verdict("Debunked", sources=())])],
            CriticOutput: [CriticOutput(weak_claims=[])],
        }
    )
    _, state = await run(build_graph(make_tools([]), llm))
    assert state["verification_results"][0]["verdict"] == "Unverified"
    assert "Lacks evidence" in state["verification_results"][0]["evidence"]


async def test_skipped_claim_still_gets_unverified_result():
    llm = FakeLLM(
        {ExtractionOutput: [EXTRACTION], VerificationOutput: [VerificationOutput(results=[])], CriticOutput: [CriticOutput(weak_claims=[])]}
    )
    _, state = await run(build_graph(make_tools([]), llm))
    assert [r["verdict"] for r in state["verification_results"]] == ["Unverified"]


async def test_tool_failure_degrades_instead_of_crashing():
    calls: List = []
    tools = make_tools(
        calls,
        {"search_web_factcheck": RuntimeError("tavily down"), "save_video_analysis": RuntimeError("clickhouse down"), "get_channel_history": RuntimeError("db down")},
    )
    llm = FakeLLM(
        {ExtractionOutput: [EXTRACTION], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]},
        tool_turns=[AIMessage(content="", tool_calls=[{"name": "search_web_factcheck", "args": {"query": "q"}, "id": "1"}]), AIMessage(content="done")],
    )
    _, state = await run(build_graph(tools, llm))
    assert state["saved_to_memory"] is False
    assert "could not be saved" in state["final_report_md"]


def test_scoring_and_helpers():
    assert compute_accuracy_score([{"verdict": "Verified"}, {"verdict": "Debunked"}, {"verdict": "Unverified"}]) == 50
    assert compute_accuracy_score([{"verdict": "Unverified"}]) == 0
    assert timestamp_to_seconds("1:02:05") == 3725 and timestamp_to_seconds("08:14") == 494 and timestamp_to_seconds("x") is None
    assert enforce_evidence({"verdict": "Verified", "sources": ["not-a-url"], "evidence": ""})["verdict"] == "Unverified"
    assert parse_tool_result([{"type": "text", "text": '{"a": 1}'}]) == {"a": 1}
    assert parse_tool_result("plain") == {"raw": "plain"}


async def test_stream_emits_human_readable_progress_events():
    llm = FakeLLM(
        {ExtractionOutput: [EXTRACTION], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]},
        tool_turns=[
            AIMessage(content="Checking the official benchmarks first.", tool_calls=[{"name": "search_web_factcheck", "args": {"query": "x faster"}, "id": "1"}]),
            AIMessage(content="done"),
        ],
    )
    events, _ = await run(build_graph(make_tools([]), llm))
    kinds = [e["type"] for e in events]

    assert {"video", "overview", "claims", "verdict", "note"} <= set(kinds)
    assert kinds.index("overview") < kinds.index("claims") < kinds.index("verdict")
    search = next(e for e in events if e["type"] == "tool_start" and e["tool"] == "search_web_factcheck")
    assert search["title"] == "Searching the web" and search["detail"] == "x faster"
    end = next(e for e in events if e["type"] == "tool_end" and e["id"] == search["id"])
    assert end["ok"] is True and "1 sources" in end["summary"]
    assert any(e["type"] == "note" and e["text"] == "Checking the official benchmarks first." for e in events)
    assert any(e["type"] == "tool_start" and e["tool"] == "thinking" for e in events)
    starts = [e["id"] for e in events if e["type"] == "tool_start"]
    ends = [e["id"] for e in events if e["type"] == "tool_end"]
    assert sorted(starts) == sorted(ends)  # every step that starts also finishes


def test_narration_describes_tools_in_plain_language():
    from agent.narration import describe_end, describe_start

    assert describe_start("search_web_factcheck", {"query": "q", "search_depth": "advanced"}) == ("Searching the web (deep search)", "q")
    title, detail = describe_start("search_social_reactions", {"topic_query": "t", "platforms": ["hackernews", "bluesky"]})
    assert "Hacker News, Bluesky" in title and detail == "t"
    assert describe_end("get_transcript", {"status": "OK", "segments": [1, 2], "language": "en"}) == (True, "2 lines (en)")
    assert describe_end("get_comments", {"status": "ERROR", "message": "quota"}) == (False, "quota")
    assert describe_end("execute_python_audit", {"stdout": "56088\n", "exit_code": 0}) == (True, "Result: 56088")

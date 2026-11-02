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


def _read_log(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_each_run_writes_a_debug_log_with_tool_details(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    calls: List = []
    llm = FakeLLM(
        {ExtractionOutput: [EXTRACTION], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]},
        tool_turns=[
            AIMessage(content="", tool_calls=[{"name": "search_web_factcheck", "args": {"query": "x faster"}, "id": "1"}]),
            AIMessage(content="done"),
        ],
    )
    events, _ = await run(build_graph(make_tools(calls), llm))

    request = next(e for e in events if e["type"] == "request")
    files = list(tmp_path.glob("*.jsonl"))
    assert [f.name for f in files] == [request["log_file"]] and request["id"] in files[0].name
    records = _read_log(files[0])
    assert {r["rid"] for r in records} == {request["id"]}
    assert records[0]["event"] == "request_start" and records[-1]["event"] == "request_end"
    assert records[-1]["status"] == "ok" and records[-1]["score"] == 35

    tool = next(r for r in records if r["event"] == "tool_call" and r["tool"] == "search_web_factcheck")
    assert tool["args"] == {"query": "x faster"} and tool["ok"] is True and tool["duration_ms"] >= 0 and "docs.x/bench" in tool["result"]
    llm_call = next(r for r in records if r["event"] == "llm_call" and "transcript" in r["purpose"])
    assert llm_call["prompt_chars"] > 0 and "clickbait" in llm_call["output"]
    assert llm_call["prompt"] is None  # full prompts only with LOG_PROMPTS=1
    assert any(r["event"] == "verdict" and r["verdict"] == "Partially False" for r in records)
    assert any(r["event"] == "llm_turn" for r in records)
    assert [r["t_ms"] for r in records] == sorted(r["t_ms"] for r in records)


async def test_failed_tool_is_logged_with_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    tools = make_tools([], {"get_channel_history": RuntimeError("db down")})
    llm = FakeLLM({ExtractionOutput: [EXTRACTION], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]})
    await run(build_graph(tools, llm))
    records = _read_log(next(tmp_path.glob("*.jsonl")))
    failed = next(r for r in records if r["event"] == "tool_call" and r["tool"] == "get_channel_history")
    assert failed["ok"] is False and "RuntimeError: db down" in failed["error"]


async def test_unhandled_error_is_logged_and_reraised(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    llm = FakeLLM({ExtractionOutput: [RuntimeError("anthropic 500")]})
    with pytest.raises(Exception, match="anthropic 500"):
        await run(build_graph(make_tools([]), llm))
    records = _read_log(next(tmp_path.glob("*.jsonl")))
    assert records[-1]["event"] == "request_error" and "anthropic 500" in records[-1]["error"]
    assert any(r["event"] == "llm_call" and r["ok"] is False for r in records)


def test_plain_text_tool_failure_is_not_reported_as_success():
    from agent.narration import describe_end

    ok, summary = describe_end("get_channel_history", {"raw": "database error fetching channel history: boom"})
    assert ok is False and "boom" in summary


async def test_unparseable_history_is_ignored_and_logged_as_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    tools = make_tools([], {"get_channel_history": "database error: boom"})
    llm = FakeLLM({ExtractionOutput: [EXTRACTION], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]})
    _, state = await run(build_graph(tools, llm))
    assert state["channel_history"] == {}
    failed = next(r for r in _read_log(next(tmp_path.glob("*.jsonl"))) if r["event"] == "tool_call" and r["tool"] == "get_channel_history")
    assert failed["ok"] is False and "boom" in failed["summary"]


def test_narration_marks_cached_results():
    from agent.narration import describe_end

    assert describe_end("get_transcript", {"status": "OK", "segments": [1], "language": "en", "cached": True}) == (True, "1 lines (en) (cached)")
    assert describe_end("get_video_metadata", {"title": "T", "channel_name": "C", "cached": True})[1].endswith("(cached)")


async def test_trace_events_stream_tool_calls_with_parameter_names_before_they_finish():
    llm = FakeLLM(
        {ExtractionOutput: [EXTRACTION], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]},
        tool_turns=[
            AIMessage(content="Checking docs.", tool_calls=[{"name": "search_web_factcheck", "args": {"query": "x faster", "search_depth": "basic"}, "id": "1"}]),
            AIMessage(content="done"),
        ],
    )
    events, _ = await run(build_graph(make_tools([]), llm))
    trace = [e["record"] for e in events if e["type"] == "trace"]
    names = [r["event"] for r in trace]

    start = next(r for r in trace if r["event"] == "tool_call_start" and r["tool"] == "search_web_factcheck")
    end = next(r for r in trace if r["event"] == "tool_call" and r["id"] == start["id"])
    assert start["args"] == {"query": "x faster", "search_depth": "basic"}  # parameter names preserved
    assert names.index("tool_call_start") < names.index("tool_call") and start["t_ms"] <= end["t_ms"]
    assert {"request_start", "llm_call_start", "llm_call", "llm_turn_start", "llm_turn", "verdict", "report_stored", "request_end"} <= set(names)
    assert trace[0]["event"] == "request_start" and trace[-1]["event"] == "request_end"
    # the start record reaches the client before the tool's result (live, not batched at the end)
    positions = {(e["record"]["event"], e["record"].get("id")): i for i, e in enumerate(events) if e["type"] == "trace"}
    assert positions[("tool_call_start", start["id"])] < positions[("tool_call", start["id"])]


async def test_successful_run_stores_report_and_aborted_run_does_not(tmp_path, monkeypatch):
    from agent.report_store import load_report

    monkeypatch.setenv("REPORT_DIR", str(tmp_path / "reports"))
    llm = FakeLLM({ExtractionOutput: [EXTRACTION], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]})
    await run(build_graph(make_tools([]), llm))
    stored = load_report("dQw4w9WgXcQ")
    assert stored["state"]["overall_accuracy_score"] == 35 and stored["video"]["title"] == META["title"]
    assert stored["state"]["verification_results"][0]["verdict"] == "Partially False"

    other = dict(META, video_id="AAAAAAAAAAA")
    aborted = make_tools([], {"get_video_metadata": other, "get_transcript": {"status": "TRANSCRIPTS_DISABLED", "segments": []}})
    await run(build_graph(aborted, FakeLLM({})))
    assert load_report("AAAAAAAAAAA") is None  # aborted analyses are never stored


def test_report_store_roundtrip_validation_and_url_parsing(tmp_path, monkeypatch):
    from agent.report_store import load_report, save_report, video_id_from_url

    monkeypatch.setenv("REPORT_DIR", str(tmp_path))
    state = {"video_id": "dQw4w9WgXcQ", "final_report_md": "# R", "overall_accuracy_score": 50, "secret_internal": "x"}
    assert save_report(state, {"title": "T"}) is not None
    got = load_report("dQw4w9WgXcQ")
    assert got["state"] == {"video_id": "dQw4w9WgXcQ", "final_report_md": "# R", "overall_accuracy_score": 50}
    assert got["video"] == {"title": "T"} and got["generated_at"]

    assert save_report({**state, "error_message": "boom"}) is None and save_report({"final_report_md": "x"}) is None
    assert load_report("not-an-id") is None and load_report("../../etc/passwd") is None
    (tmp_path / "BBBBBBBBBBB.json").write_text("{corrupt", encoding="utf-8")
    assert load_report("BBBBBBBBBBB") is None

    for url in ["https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5s", "https://youtu.be/dQw4w9WgXcQ?si=x", "https://www.youtube.com/shorts/dQw4w9WgXcQ"]:
        assert video_id_from_url(url) == "dQw4w9WgXcQ"
    assert video_id_from_url("https://evil.example/watch?v=dQw4w9WgXcQ") is None and video_id_from_url("junk") is None

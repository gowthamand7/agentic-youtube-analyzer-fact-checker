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
    Claim,
    ClaimVerdict,
    ClickbaitBuster,
    CommentInsight,
    CommentsAnalysis,
    CriticOutput,
    ExtraItem,
    ExtraSection,
    KeyPoint,
    SponsorMention,
    SynthesisOutput,
    Topic,
    VerificationOutput,
    VideoAnalysis,
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


ANALYSIS = VideoAnalysis(
    video_type="tutorial",
    tldr="Library X is faster than Y, but nowhere near 4x.",
    key_points=[
        KeyPoint(timestamp="02:00", point="Library X caches parsed results between calls.", concept="X result caching", importance="medium"),
        KeyPoint(timestamp="04:32", point="Library X is 4x faster than Y.", concept="X vs Y speed", importance="high", claim_index=0),
    ],
    clickbait=ClickbaitBuster(verdict="Only about 1.4x.", payoff_timestamp="08:14", clickbait_rating="Medium"),
    claims=[Claim(timestamp="04:32", claim="Library X is 4x faster than Y.", importance="high", quantitative=False)],
    sponsors=[SponsorMention(name="NordVPN", timestamp_range="02:10 - 03:45", disclosed=False)],
    has_paid_promotion=True,
    disclosed_properly=False,
    bias_notes="Praises X without noting affiliate link.",
    topic_tags=["python"],
    extra_sections=[ExtraSection(title="Steps to follow", kind="steps", items=[ExtraItem(label="1", text="pip install x")])],
)
COMMENTS = CommentsAnalysis(
    insights=[CommentInsight(category="Tutorial Fix", author="@dev", likes=5, concept="legacy peer deps flag", detail="Use --legacy-peer-deps on Node 22.", adds_information=True)]
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
        {VideoAnalysis: [ANALYSIS], CommentsAnalysis: [COMMENTS], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]},
        tool_turns=[
            AIMessage(content="", tool_calls=[{"name": "search_web_factcheck", "args": {"query": "x"}, "id": "1"}]),
            AIMessage(content="done"),
        ],
    )
    events, state = await run(build_graph(make_tools(calls), llm))

    assert state["overall_accuracy_score"] == 35
    assert state["saved_to_memory"] is True
    md = state["final_report_md"]
    assert "## Summary" in md and "## Fact check" in md and "## Before you watch" in md
    assert "Library X is 4x faster than Y." in md and "NordVPN" in md
    assert state["report"]["reliability"]["contradicted"] == 1
    saved = json.loads(next(a for n, a in calls if n == "save_video_analysis")["payload_json"])
    assert saved["channel_id"] == "UC1" and saved["overall_accuracy_score"] == 35
    assert saved["sponsors"] == ["NordVPN"] and saved["analysis_payload"]["claims_verified"][0]["verdict"] == "Partially False"
    assert [e["node"] for e in events if e["type"] == "node_end"] == ["triage", "extraction", "investigator", "critic", "synthesis", "commit_and_report"]
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
            VideoAnalysis: [ANALYSIS],
            CommentsAnalysis: [COMMENTS],
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
            VideoAnalysis: [ANALYSIS],
            CommentsAnalysis: [COMMENTS],
            VerificationOutput: [VerificationOutput(results=[verdict("Debunked", sources=())])],
            CriticOutput: [CriticOutput(weak_claims=[])],
        }
    )
    _, state = await run(build_graph(make_tools([]), llm))
    assert state["verification_results"][0]["verdict"] == "Unverified"
    assert "Lacks evidence" in state["verification_results"][0]["evidence"]


async def test_skipped_claim_still_gets_unverified_result():
    llm = FakeLLM(
        {VideoAnalysis: [ANALYSIS], CommentsAnalysis: [COMMENTS], VerificationOutput: [VerificationOutput(results=[])], CriticOutput: [CriticOutput(weak_claims=[])]}
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
        {VideoAnalysis: [ANALYSIS], CommentsAnalysis: [COMMENTS], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]},
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
        {VideoAnalysis: [ANALYSIS], CommentsAnalysis: [COMMENTS], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]},
        tool_turns=[
            AIMessage(content="Checking the official benchmarks first.", tool_calls=[{"name": "search_web_factcheck", "args": {"query": "x faster"}, "id": "1"}]),
            AIMessage(content="done"),
        ],
    )
    events, _ = await run(build_graph(make_tools([]), llm))
    kinds = [e["type"] for e in events]

    assert {"video", "summary", "overview", "claims", "comments", "verdict", "note"} <= set(kinds)
    assert kinds.index("summary") < kinds.index("claims") < kinds.index("verdict")
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
        {VideoAnalysis: [ANALYSIS], CommentsAnalysis: [COMMENTS], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]},
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
    llm_call = next(r for r in records if r["event"] == "llm_call" and "Summarizing" in r["purpose"])
    assert llm_call["prompt_chars"] > 0 and "clickbait" in llm_call["output"]
    assert llm_call["prompt"] is None  # full prompts only with LOG_PROMPTS=1
    assert any(r["event"] == "verdict" and r["verdict"] == "Partially False" for r in records)
    assert any(r["event"] == "llm_turn" for r in records)
    assert [r["t_ms"] for r in records] == sorted(r["t_ms"] for r in records)


async def test_failed_tool_is_logged_with_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    tools = make_tools([], {"get_channel_history": RuntimeError("db down")})
    llm = FakeLLM({VideoAnalysis: [ANALYSIS], CommentsAnalysis: [COMMENTS], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]})
    await run(build_graph(tools, llm))
    records = _read_log(next(tmp_path.glob("*.jsonl")))
    failed = next(r for r in records if r["event"] == "tool_call" and r["tool"] == "get_channel_history")
    assert failed["ok"] is False and "RuntimeError: db down" in failed["error"]


async def test_unhandled_error_is_logged_and_reraised(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    llm = FakeLLM({VideoAnalysis: [RuntimeError("anthropic 500")]})
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
    llm = FakeLLM({VideoAnalysis: [ANALYSIS], CommentsAnalysis: [COMMENTS], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]})
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
        {VideoAnalysis: [ANALYSIS], CommentsAnalysis: [COMMENTS], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]},
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
    llm = FakeLLM({VideoAnalysis: [ANALYSIS], CommentsAnalysis: [COMMENTS], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]})
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


async def test_low_importance_weak_claims_do_not_trigger_retries():
    low = ANALYSIS.model_copy(update={"claims": [Claim(timestamp="04:32", claim="Library X is 4x faster than Y.", importance="low", quantitative=False)]})
    weak = verdict("Unverified", sources=(), conf="low")
    llm = FakeLLM({VideoAnalysis: [low], CommentsAnalysis: [COMMENTS], VerificationOutput: [VerificationOutput(results=[weak])], CriticOutput: [CriticOutput(weak_claims=[])]})
    _, state = await run(build_graph(make_tools([]), llm))
    assert state["retry_count"] == 0 and [c for c in llm.structured_calls if c is VerificationOutput] == [VerificationOutput]
    assert state["verification_results"][0]["importance"] == "low"


async def test_summary_and_comment_analysis_run_and_comment_failure_is_tolerated():
    llm = FakeLLM(
        {
            VideoAnalysis: [ANALYSIS],
            CommentsAnalysis: [RuntimeError("comments model down")],
            VerificationOutput: [VerificationOutput(results=[verdict()])],
            CriticOutput: [CriticOutput(weak_claims=[])],
        }
    )
    events, state = await run(build_graph(make_tools([]), llm))
    assert VideoAnalysis in llm.structured_calls and CommentsAnalysis in llm.structured_calls
    assert state["comment_insights"] == [] and state["report"]["comments"] == []
    assert state["report"]["summary"]["tldr"] and state["overall_accuracy_score"] == 35
    assert not any(e["type"] == "comments" for e in events)


async def test_report_carries_structure_comments_and_importance():
    llm = FakeLLM({VideoAnalysis: [ANALYSIS], CommentsAnalysis: [COMMENTS], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]})
    events, state = await run(build_graph(make_tools([]), llm))
    report = state["report"]
    assert report["version"] == 3 and report["video_type"] == "tutorial"
    assert [t["title"] for t in report["topics"]] == ["X result caching", "X vs Y speed"]  # no synthesis output scripted: key ideas stand in
    assert any(e["type"] == "note" and "topic summary could not be written" in e["text"] for e in events)
    assert report["summary"]["key_points"][1]["check"]["verdict"] == "Partially False"
    assert report["comments"][0]["author"] == "@dev" and report["extras"][0]["kind"] == "steps"
    summary_event = next(e for e in events if e["type"] == "summary")
    assert summary_event["tldr"] == ANALYSIS.tldr and len(summary_event["key_points"]) == 2


class FlakyOnce:
    """Raises a pydantic ValidationError on the first structured call, then returns the real object."""

    def __init__(self, good):
        self.good, self.calls = good, 0


async def test_structured_output_that_misses_a_field_is_retried_with_the_error(tmp_path, monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    try:
        VideoAnalysis.model_validate({"video_type": "tutorial"})
    except ValidationError as err:
        validation_error = err
    llm = FakeLLM({VideoAnalysis: [validation_error, ANALYSIS], CommentsAnalysis: [COMMENTS], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]})
    sent = []
    original = llm.with_structured_output

    def spy(schema):
        inner = original(schema)

        class _S:
            async def ainvoke(self, messages):
                sent.append((schema.__name__, len(messages)))
                return await inner.ainvoke(messages)

        return _S()

    llm.with_structured_output = spy
    _, state = await run(build_graph(make_tools([]), llm))
    analysis_calls = [n for name, n in sent if name == "VideoAnalysis"]
    assert len(analysis_calls) == 2 and analysis_calls[1] == analysis_calls[0] + 1  # second call carries the error message
    assert state["report"]["summary"]["tldr"] and "structured_retry" in [r["event"] for r in _read_log(next(tmp_path.glob("*.jsonl")))]


async def test_missing_topic_tags_fall_back_to_key_idea_concepts():
    no_tags = ANALYSIS.model_copy(update={"topic_tags": []})
    llm = FakeLLM({VideoAnalysis: [no_tags], CommentsAnalysis: [COMMENTS], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]})
    _, state = await run(build_graph(make_tools([]), llm))
    assert state["report"]["topic_tags"] == ["x result caching", "x vs y speed"]


def test_drop_empty_sections_removes_absence_filler():
    from agent.nodes import ABSENCE, drop_empty_sections

    sections = [
        {"title": "Numbers to know", "kind": "list", "items": [{"label": "", "text": "The video contains no numerical statistics or benchmarks."}]},
        {"title": "Steps", "kind": "steps", "items": [{"label": "1", "text": "Install it."}, {"label": "", "text": "None."}, {"label": "", "text": "  "}]},
        {"title": "Empty", "kind": "list", "items": []},
    ]
    assert drop_empty_sections(sections) == [{"title": "Steps", "kind": "steps", "items": [{"label": "1", "text": "Install it."}]}]
    for filler in ["No affiliate links, promo codes or sponsors are present.", "None", "This video has no sponsors", "Nothing notable"]:
        assert ABSENCE.match(filler)
    assert not ABSENCE.match("Notable bias: the host praises the product he sells.")
    assert not ABSENCE.match("Nobody asked, but...")  # only whole words count


async def test_filler_bias_note_and_sections_never_reach_the_report():
    filler = ANALYSIS.model_copy(update={
        "bias_notes": "No affiliate links, promo codes or sponsors are present.",
        "extra_sections": [ExtraSection(title="Numbers to know", kind="list", items=[ExtraItem(text="No numbers appear in this video.")])],
    })
    llm = FakeLLM({VideoAnalysis: [filler], CommentsAnalysis: [COMMENTS], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]})
    _, state = await run(build_graph(make_tools([]), llm))
    assert state["report"]["extras"] == [] and state["report"]["before_you_watch"]["bias_notes"] == ""
    assert "Numbers to know" not in state["final_report_md"]


async def test_comments_that_add_no_information_are_dropped():
    comments = CommentsAnalysis(insights=[
        CommentInsight(category="Extra Context", author="@a", likes=52, concept="course request", detail="Wants a full course.", adds_information=False),
        CommentInsight(category="Correction", author="@b", likes=3, concept="real fix", detail="Use flag --x.", adds_information=True),
    ])
    llm = FakeLLM({VideoAnalysis: [ANALYSIS], CommentsAnalysis: [comments], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]})
    events, state = await run(build_graph(make_tools([]), llm))
    assert [c["author"] for c in state["report"]["comments"]] == ["@b"]
    assert [i["author"] for i in next(e for e in events if e["type"] == "comments")["insights"]] == ["@b"]


async def test_key_points_are_shown_in_video_order():
    shuffled = ANALYSIS.model_copy(update={"key_points": [
        KeyPoint(timestamp="10:00", point="Late point.", concept="late", importance="low"),
        KeyPoint(timestamp="01:30", point="Early point.", concept="early", importance="low"),
        KeyPoint(timestamp="1:05:00", point="Very late point.", concept="very late", importance="low"),
        KeyPoint(timestamp="??", point="Unparseable time.", concept="odd", importance="low"),
        KeyPoint(timestamp="00:00", point="Opening point.", concept="opening", importance="low"),
    ]})
    llm = FakeLLM({VideoAnalysis: [shuffled], CommentsAnalysis: [COMMENTS], VerificationOutput: [VerificationOutput(results=[verdict()])], CriticOutput: [CriticOutput(weak_claims=[])]})
    _, state = await run(build_graph(make_tools([]), llm))
    assert [k["point"] for k in state["report"]["summary"]["key_points"]] == ["Opening point.", "Early point.", "Late point.", "Very late point.", "Unparseable time."]


def test_describe_end_names_the_supadata_fallback():
    from agent.narration import describe_end
    base = {"status": "OK", "segments": [1, 2], "language": "en", "source": "supadata"}
    assert describe_end("get_transcript", {**base, "fallback_for": "REQUEST_BLOCKED"}) == (
        True, "2 lines (en) via Supadata fallback (YouTube blocked the scraper)")
    assert describe_end("get_transcript", {**base, "fallback_for": "TRANSCRIPT_ERROR", "cached": True}) == (
        True, "2 lines (en) via Supadata fallback (the scraper failed) (cached)")


TOPICS = SynthesisOutput(
    overview="Library X is about 1.4x faster than Y, not 4x.",
    topics=[
        Topic(title="The speed claim", timestamp="04:32", summary="The host says X is 4x faster, but benchmarks show about 1.4x.",
              source_urls=["https://docs.x/bench", "https://invented.example/nope"]),
        Topic(title="Empty", timestamp="05:00", summary="   ", source_urls=[]),
    ]
)


class SpyLLM(FakeLLM):
    """FakeLLM that also records the messages sent for each structured schema."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.sent: Dict[type, str] = {}

    def with_structured_output(self, schema):
        inner = super().with_structured_output(schema)
        llm = self

        class _S:
            async def ainvoke(self, messages):
                llm.sent[schema] = "\n".join(str(m.content) for m in messages)
                return await inner.ainvoke(messages)

        return _S()


def full_llm(synthesis):
    return SpyLLM({
        VideoAnalysis: [ANALYSIS], CommentsAnalysis: [COMMENTS], VerificationOutput: [VerificationOutput(results=[verdict()])],
        CriticOutput: [CriticOutput(weak_claims=[])], SynthesisOutput: [synthesis],
    })


async def test_synthesis_writes_topics_from_transcript_evidence_and_comments_and_drops_invented_urls():
    llm = full_llm(TOPICS)
    _, state = await run(build_graph(make_tools([]), llm))
    assert [t["title"] for t in state["topics"]] == ["The speed claim"]  # the blank paragraph is dropped
    assert state["report"]["summary"]["tldr"] == "Library X is about 1.4x faster than Y, not 4x."  # the first-party overview replaces the analysis summary
    assert state["topics"][0]["source_urls"] == ["https://docs.x/bench"]  # a URL no tool returned never reaches the report
    prompt = llm.sent[SynthesisOutput]
    assert "4x faster" in prompt  # transcript
    assert "1.4x average." in prompt and "https://docs.x/bench" in prompt  # fact-check evidence and its source
    assert "legacy-peer-deps" in prompt  # useful point from the comments
    md = state["final_report_md"]
    assert "### The speed claim" in md and "[1](https://docs.x/bench)" in md


async def test_synthesis_failure_falls_back_to_key_ideas_and_does_not_fail_the_run():
    llm = full_llm(RuntimeError("model down"))
    _, state = await run(build_graph(make_tools([]), llm))
    assert state["saved_to_memory"] is True and not state.get("error_message")
    assert [t["summary"] for t in state["topics"]] == [kp.point for kp in ANALYSIS.key_points]
    assert state["report"]["summary"]["tldr"] == ANALYSIS.tldr  # no overview was written, so the analysis summary stands in


async def test_hundred_comments_with_replies_are_fetched_and_shown_to_the_model():
    calls: List = []
    comments = {"status": "OK", "comments": [
        {"author": "@dev", "text": "use --legacy-peer-deps", "likes": 5, "is_creator": False, "replies": [
            {"author": "@helper", "text": "that fixed it for me on Node 22", "likes": 2, "is_creator": False},
        ]},
    ]}
    llm = full_llm(TOPICS)
    await run(build_graph(make_tools(calls, {"get_comments": comments}), llm))
    args = next(a for n, a in calls if n == "get_comments")
    assert args["max_results"] == 100 and args["sort_by"] == "top"
    assert "  > @helper [2 likes]: that fixed it for me on Node 22" in llm.sent[CommentsAnalysis]


def test_clickbait_rating_accepts_the_alias_the_model_actually_used():
    # Seen live: the model answered {"verdict": ..., "rating": "Medium"} twice, which aborted a whole run.
    parsed = ClickbaitBuster.model_validate({"verdict": "Yes in part", "rating": "Medium"})
    assert parsed.clickbait_rating == "Medium" and parsed.payoff_timestamp == ""
    assert ClickbaitBuster.model_validate({"verdict": "v", "clickbait_rating": "high"}).clickbait_rating == "High"
    assert ClickbaitBuster.model_validate({"verdict": "v", "rating": "None"}).clickbait_rating == "Low"
    with pytest.raises(Exception):
        ClickbaitBuster.model_validate({"verdict": "v"})  # a genuinely missing rating is still an error

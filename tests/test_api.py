"""API tests with a fake agent runner (no network, no MCP servers)."""

from __future__ import annotations

from typing import Any, AsyncGenerator, Dict

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_runner

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


async def fake_runner(video_url: str) -> AsyncGenerator[Dict[str, Any], None]:
    yield {"type": "node_start", "node": "triage"}
    yield {"type": "tool_start", "id": "a1", "tool": "get_video_metadata", "title": "Reading the video's details", "detail": None}
    yield {"type": "tool_end", "id": "a1", "tool": "get_video_metadata", "ok": True, "summary": "“T” by C"}
    yield {"type": "note", "text": "Investigating: “x”"}
    yield {"type": "something_internal", "secret": 1}
    yield {"type": "node_end", "node": "triage"}
    yield {
        "type": "final",
        "state": {
            "final_report_md": "\n".join(
                ["# Report", "", "| a | b |", "| - | - |", "| 1 | 2 |", "", "<script>alert(1)</script>", "", "[x](javascript:alert(1))"]
            ),
            "overall_accuracy_score": 77,
            "saved_to_memory": True,
        },
    }


async def failing_runner(video_url: str) -> AsyncGenerator[Dict[str, Any], None]:
    yield {"type": "node_start", "node": "triage"}
    raise RuntimeError("mcp exploded")


@pytest.fixture
def client():
    app.dependency_overrides[get_runner] = lambda: fake_runner
    yield TestClient(app)
    app.dependency_overrides.clear()


def parse_sse(body: str):
    events = []
    for block in body.strip().split("\n\n"):
        ev = {"event": "message", "data": []}
        for line in block.splitlines():
            key, _, value = line.partition(":")
            if key == "event":
                ev["event"] = value.strip()
            elif key == "data":
                ev["data"].append(value[1:] if value.startswith(" ") else value)
        events.append((ev["event"], "\n".join(ev["data"])))
    return events


def test_stream_headers_and_framing(client):
    with client.stream("GET", "/api/analyze/stream", params={"video_url": URL}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache"
        body = "".join(r.iter_text())
    events = parse_sse(body)
    assert [e for e, _ in events] == ["agent"] * 5 + ["report", "done"]
    payloads = [json.loads(d) for e, d in events if e == "agent"]
    assert [p["type"] for p in payloads] == ["node_start", "tool_start", "tool_end", "note", "node_end"]
    assert payloads[1]["title"] == "Reading the video's details" and payloads[2]["summary"] == "“T” by C"
    assert "something_internal" not in body  # unknown event types are not forwarded


def test_stream_report_is_rendered_and_sanitized(client):
    with client.stream("GET", "/api/analyze/stream", params={"video_url": URL}) as r:
        events = parse_sse("".join(r.iter_text()))
    report = dict((e, d) for e, d in events)["report"]
    assert "<h1>Report</h1>" in report and "<table>" in report
    assert "<script>" not in report
    assert 'href="javascript:' not in report
    assert "77" in report


def test_stream_rejects_non_youtube_url(client):
    assert client.get("/api/analyze/stream", params={"video_url": "https://evil.example/x"}).status_code == 422
    assert client.get("/api/analyze/stream").status_code == 422


def test_stream_reports_runner_failure_as_error_event():
    app.dependency_overrides[get_runner] = lambda: failing_runner
    try:
        with TestClient(app).stream("GET", "/api/analyze/stream", params={"video_url": URL}) as r:
            events = parse_sse("".join(r.iter_text()))
    finally:
        app.dependency_overrides.clear()
    names = [e for e, _ in events]
    assert "error" in names and names[-1] == "done"
    assert "mcp exploded" in dict(events)["error"]


def test_post_analyze_returns_json(client):
    r = client.post("/api/analyze", json={"video_url": URL})
    assert r.status_code == 200
    body = r.json()
    assert body["overall_accuracy_score"] == 77 and body["saved_to_memory"] is True
    assert body["report_md"].startswith("# Report")
    assert body["error_message"] is None


def test_post_analyze_validates(client):
    assert client.post("/api/analyze", json={"video_url": "nope"}).status_code == 422


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "EventSource" in r.text


def test_report_renders_claims_as_accordion_and_escapes():
    from app.main import report_html

    md = "\n".join(
        [
            "# R",
            "",
            "## Scorecard",
            "| Timestamp | Host's Claim | Verdict | Verification & Evidence |",
            "| :--- | :--- | :--- | :--- |",
            "| 1 | c | v | e |",
            "",
            "## Next",
            "",
        ]
    )
    state = {
        "video_id": "dQw4w9WgXcQ",
        "final_report_md": md,
        "overall_accuracy_score": 40,
        "verification_results": [
            {
                "timestamp": "04:32",
                "claim": "<b>4x faster</b>",
                "verdict": "Partially False",
                "evidence": "1.4x <img src=x onerror=1>",
                "sources": ["https://a.example/p?x=1&y=2", "javascript:alert(1)"],
                "confidence": "high",
            }
        ],
    }
    out = report_html(state)
    assert out.count("<details") == 1 and "<table>" not in out and "[[CLAIMS]]" not in out
    assert "&lt;b&gt;4x faster&lt;/b&gt;" in out and "<img" not in out
    assert "t=272s" in out and "javascript:" not in out
    assert "<h2>Next</h2>" in out


# ---- stored reports ---------------------------------------------------------------------------------
def _seed_report():
    from agent.report_store import save_report

    save_report(
        {
            "video_id": "dQw4w9WgXcQ",
            "final_report_md": "# Saved report",
            "overall_accuracy_score": 61,
            "saved_to_memory": True,
            "verification_results": [],
        },
        {"title": "Saved title", "channel": "Chan"},
    )


def _counting_runner(calls):
    async def runner(video_url):
        calls.append(video_url)
        yield {"type": "final", "state": {"final_report_md": "# Fresh report", "overall_accuracy_score": 99}}

    return runner


def test_stored_report_is_served_without_running_the_agent():
    _seed_report()
    calls = []
    app.dependency_overrides[get_runner] = lambda: _counting_runner(calls)
    try:
        with TestClient(app).stream("GET", "/api/analyze/stream", params={"video_url": URL}) as r:
            events = parse_sse("".join(r.iter_text()))
    finally:
        app.dependency_overrides.clear()
    names = [e for e, _ in events]
    assert names == ["cached", "report", "done"] and calls == []
    meta = json.loads(dict(events)["cached"])
    assert meta["video"]["title"] == "Saved title" and meta["generated_at"] and meta["video_id"] == "dQw4w9WgXcQ"
    assert "Saved report" in dict(events)["report"] and "61/100" in dict(events)["report"]


def test_refresh_ignores_the_stored_report_and_runs_the_agent():
    _seed_report()
    calls = []
    app.dependency_overrides[get_runner] = lambda: _counting_runner(calls)
    try:
        with TestClient(app).stream("GET", "/api/analyze/stream", params={"video_url": URL, "refresh": "true"}) as r:
            events = parse_sse("".join(r.iter_text()))
    finally:
        app.dependency_overrides.clear()
    assert calls == [URL] and [e for e, _ in events] == ["report", "done"] and "Fresh report" in dict(events)["report"]


def test_post_analyze_reuses_stored_report_unless_refresh():
    _seed_report()
    calls = []
    app.dependency_overrides[get_runner] = lambda: _counting_runner(calls)
    try:
        client = TestClient(app)
        cached = client.post("/api/analyze", json={"video_url": URL}).json()
        fresh = client.post("/api/analyze", json={"video_url": URL, "refresh": True}).json()
    finally:
        app.dependency_overrides.clear()
    assert cached["cached"] is True and cached["generated_at"] and cached["report_md"] == "# Saved report"
    assert fresh["cached"] is False and fresh["report_md"] == "# Fresh report" and calls == [URL]


def test_trace_events_are_forwarded_to_the_client():
    async def runner(video_url):
        yield {"type": "trace", "record": {"event": "tool_call_start", "tool": "t", "args": {"query": "q"}, "t_ms": 5}}
        yield {"type": "final", "state": {"final_report_md": "# R"}}

    app.dependency_overrides[get_runner] = lambda: runner
    try:
        with TestClient(app).stream("GET", "/api/analyze/stream", params={"video_url": URL}) as r:
            events = parse_sse("".join(r.iter_text()))
    finally:
        app.dependency_overrides.clear()
    trace = json.loads(events[0][1])
    assert trace["type"] == "trace" and trace["record"]["args"] == {"query": "q"}

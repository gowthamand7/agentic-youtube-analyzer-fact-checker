"""API tests with a fake agent runner (no network, no MCP servers)."""

from __future__ import annotations

from typing import Any, AsyncGenerator, Dict

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_runner

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


async def fake_runner(video_url: str) -> AsyncGenerator[Dict[str, Any], None]:
    yield {"type": "node_start", "node": "triage"}
    yield {"type": "tool_start", "tool": "get_video_metadata"}
    yield {"type": "tool_end", "tool": "get_video_metadata", "ok": True}
    yield {"type": "node_end", "node": "triage"}
    yield {
        "type": "final",
        "state": {
            "final_report_md": "# Report\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n<script>alert(1)</script>\n\n[x](javascript:alert(1))",
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
    names = [e for e, _ in events]
    assert names[0] == "progress"
    assert names[-2:] == ["report", "done"]
    progress = " ".join(d for e, d in events if e == "progress")
    assert "Triage" in progress and "get_video_metadata" in progress


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


def test_ui_panel_embeds_sse_connection(client):
    r = client.get("/ui/panel", params={"video_url": URL})
    assert r.status_code == 200
    assert 'hx-ext="sse"' in r.text
    assert "sse-connect=\"/api/analyze/stream?video_url=https%3A%2F%2Fwww.youtube.com" in r.text
    assert 'sse-close="done"' in r.text


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "htmx" in r.text.lower()


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

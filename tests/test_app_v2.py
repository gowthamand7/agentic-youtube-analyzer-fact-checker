"""v2 HTML rendering and delivery of stored and fresh reports (no network)."""

from __future__ import annotations

import html as htmllib
import json

import pytest
from fastapi.testclient import TestClient

from agent.report import build_report, compute_accuracy_score
from agent.report_store import save_report
from app.main import app, get_runner
from app.render import render_report_html
from test_report_v2 import result, state_for_report

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
VID = "dQw4w9WgXcQ"


def sample_report(**state_overrides):
    state = {**state_for_report(), **state_overrides}
    results = [result("Debunked", "high", claim="A is 4x"), result("Verified", "low", claim="B costs $5")]
    return build_report(state, results, compute_accuracy_score(results))


def seed(report=None):
    report = report or sample_report()
    save_report(
        {"video_id": VID, "final_report_md": "# md", "overall_accuracy_score": 75, "verification_results": [], "report": report},
        {"title": report["title"], "channel": report["channel"]},
    )
    return report


@pytest.fixture
def client():
    yield TestClient(app)
    app.dependency_overrides.clear()


def parse_sse(body: str):
    out = []
    for block in body.strip().split("\n\n"):
        ev, data = "message", []
        for line in block.splitlines():
            key, _, value = line.partition(":")
            if key == "event":
                ev = value.strip()
            elif key == "data":
                data.append(value[1:] if value.startswith(" ") else value)
        out.append((ev, "\n".join(data)))
    return out


# ---- renderer -------------------------------------------------------------------------------------
def test_render_has_all_sections_in_order_and_dynamic_extras():
    html = render_report_html(sample_report())
    order = [htmllib.escape("What you'll learn"), htmllib.escape("What's wrong or doubtful"), "From the comments", "Elsewhere on the web", "Steps", "fact-checked claims", "Before you watch"]
    positions = [html.index(name) for name in order]
    assert positions == sorted(positions)
    assert 'data-video-id="dQw4w9WgXcQ"' in html and "<ol class=\"extra extra-steps\">" in html
    assert "t=60s" in html  # timestamp deep links


def test_render_escapes_everything_and_skips_unsafe_urls():
    report = sample_report()
    report["title"] = "<script>alert(1)</script>"
    report["summary"]["tldr"] = "<img src=x onerror=alert(2)>"
    report["summary"]["key_points"][0]["point"] = "</li><b>bold</b>"
    report["corrections"][0]["sources"] = ["https://ok.example/p?a=1&b=2", "javascript:alert(3)"]
    report["outside_discussion"][0]["url"] = "javascript:alert(4)"
    html = render_report_html(report)
    assert "<script>" not in html and "<img" not in html and "<b>bold</b>" not in html
    assert "javascript:" not in html and "a=1&amp;b=2" in html


def test_summary_and_comments_events_are_forwarded(client):
    async def runner(url):
        yield {"type": "summary", "tldr": "t", "key_points": []}
        yield {"type": "comments", "insights": [{"detail": "d"}]}
        yield {"type": "final", "state": {"final_report_md": "# R"}}

    app.dependency_overrides[get_runner] = lambda: runner
    with client.stream("GET", "/api/analyze/stream", params={"video_url": URL}) as r:
        events = parse_sse("".join(r.iter_text()))
    assert [json.loads(d)["type"] for e, d in events if e == "agent"] == ["summary", "comments"]


def test_legacy_stored_reports_still_render(client):
    save_report({"video_id": VID, "final_report_md": "# Old format report", "overall_accuracy_score": 61, "verification_results": []}, {"title": "Old"})
    with client.stream("GET", "/api/analyze/stream", params={"video_url": URL}) as r:
        html = dict(parse_sse("".join(r.iter_text())))["report"]
    assert "Old format report" in html and "61/100" in html


def test_post_analyze_returns_the_structured_report(client):
    seed()
    body = client.post("/api/analyze", json={"video_url": URL}).json()
    assert body["cached"] is True and body["report"]["version"] == 2

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
def test_render_has_a_header_strip_two_tabs_topics_and_a_plain_fact_table():
    html = render_report_html(sample_report())
    strip = html.split('role="tablist"')[0]
    assert "review" in strip and "Clickbait: No" in strip and 'class="chip good">Clickbait' in strip  # Low is shown as "No", in green
    assert 'title="1 of 2 checked claims hold up' in strip and "25% of facts correct" in strip and "Paid promotion: NordVPN" in strip  # high Debunked + low Verified -> 1 of 4 weight
    assert html.count('role="tab"') == 2 and 'data-tab="summary"' in html and 'data-tab="facts"' in html
    summary, facts = html.split('id="rpanel-facts"')
    assert "Speed claims" in summary and "benchmarks show 1.4x" in summary and "a.example" in summary and "t=60s" in summary
    assert "<table" not in summary and "What's wrong" not in html and "I already know" not in html
    assert facts.count("<tr>") == 3  # header row + one row per checked claim
    assert "verdict-bad" in facts and ">False<" in facts and ">Correct<" in facts and "A is 4x" in facts and "a.example" in facts
    assert "hidden" in facts[:120]  # the Fact check tab starts hidden


def test_clickbait_levels_and_missing_checks_have_their_own_colours():
    for rating, label, cls in (("Medium", "Medium", "warn"), ("High", "High", "bad")):
        report = sample_report()
        report["headline"]["clickbait_rating"] = rating
        assert f'class="chip {cls}">Clickbait: {label}' in render_report_html(report)
    report = sample_report()
    report["checked"], report["fact_check"] = [], []
    html = render_report_html(report)
    assert "Facts correct: n/a" in html and "No claims in this video could be fact-checked." in html


def test_render_escapes_everything_and_skips_unsafe_urls():
    report = sample_report()
    report["title"] = "<script>alert(1)</script>"
    report["summary"]["tldr"] = "<img src=x onerror=alert(2)>"
    report["topics"][0]["summary"] = "</p><b>bold</b>"
    report["topics"][0]["title"] = "<i>t</i>"
    report["topics"][0]["source_urls"] = ["https://ok.example/p?a=1&b=2", "javascript:alert(3)"]
    report["fact_check"][0]["claim"] = "<u>claim</u>"
    report["fact_check"][0]["sources"] = ["javascript:alert(4)", "https://ok.example/f"]
    html = render_report_html(report)
    assert "<script>" not in html and "<img" not in html and "<b>bold</b>" not in html and "<i>" not in html and "<u>" not in html
    assert "javascript:" not in html and "a=1&amp;b=2" in html and "ok.example/f" in html


def test_reports_saved_before_v3_still_render_with_a_hint():
    report = sample_report()
    for key in ("topics", "fact_check"):
        del report[key]
    html = render_report_html(report)
    assert "predates topic summaries" in html and "A speed" in html  # key ideas stand in for topics
    assert "A is 4x" in html.split('id="rpanel-facts"')[1]  # the table is derived from the checked claims


def test_topic_step_failure_shows_key_ideas_without_the_hint():
    report = sample_report()
    report["topics"] = []
    html = render_report_html(report)
    assert "A speed" in html and "predates" not in html


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
    assert body["cached"] is True and body["report"]["version"] == 3


def test_sponsor_chip_keeps_names_short_and_the_full_text_in_the_tooltip():
    report = sample_report()
    report["before_you_watch"]["sponsors"] = [
        {"name": "Amazon Associates (affiliate links for mic, camera)", "disclosed": False},
        {"name": "DataCamp", "disclosed": True}, {"name": "Maven, promo codes", "disclosed": True}, {"name": "Scrimba", "disclosed": True},
    ]
    html = render_report_html(report)
    assert ">Paid promotion: Amazon Associates, DataCamp, Maven +1</span>" in html
    assert 'title="Amazon Associates (affiliate links for mic, camera); DataCamp; Maven, promo codes; Scrimba"' in html

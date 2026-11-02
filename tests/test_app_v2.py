"""v2 HTML rendering, personalization on delivery, and the "I already know this" API (no network)."""

from __future__ import annotations

import html as htmllib
import json

import pytest
from fastapi.testclient import TestClient

from agent.knowledge import KnowledgeStore
from agent.report import build_report, compute_accuracy_score
from agent.report_store import save_report
from app.main import app, get_personalizer, get_runner
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
    assert html.count('class="know-btn"') == 5  # 4 key points + 1 comment
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


def test_render_hides_known_items_in_a_collapsed_group_and_counts_new_ones():
    report = sample_report()
    kp0, kp1 = report["summary"]["key_points"][0], report["summary"]["key_points"][1]
    notes = {kp0["id"]: {"known": True, "entry_id": "abc1234567", "concept": "A speed"}, report["comments"][0]["id"]: {"known": True, "entry_id": "def1234567", "concept": "A is not 4x"}}
    html = render_report_html(report, notes)
    learn = html.split(htmllib.escape("What you'll learn"))[1].split(htmllib.escape("What's wrong"))[0]
    assert "2 new to you" not in learn and "3 new to you" in learn
    shown, group = learn.split('class="known-group"')
    assert kp0["point"] in group and kp0["point"] not in shown  # the known point is only in the collapsed group
    assert kp1["point"] in shown and 'data-action="unknow" data-entry="abc1234567"' in group
    assert "You already know everything the comments add." in html and "You already know 1 of these comments" in html


def test_render_says_so_when_everything_is_already_known():
    report = sample_report()
    notes = {kp["id"]: {"known": True, "entry_id": "abc1234567", "concept": "x"} for kp in report["summary"]["key_points"]}
    assert "You already know everything this video teaches" in render_report_html(report, notes)


# ---- knowledge API --------------------------------------------------------------------------------
def test_mark_known_uses_server_side_text_is_idempotent_and_lists(client):
    report = seed()
    kp = report["summary"]["key_points"][2]
    r = client.post("/api/knowledge", json={"video_id": VID, "item_id": kp["id"], "text": "FORGED", "concept": "forged"})
    assert r.status_code == 200
    entry = r.json()
    assert entry["concept"] == kp["concept"] and entry["text"] == kp["point"] and entry["video_id"] == VID  # client text ignored
    assert client.post("/api/knowledge", json={"video_id": VID, "item_id": kp["id"]}).json()["id"] == entry["id"]
    assert [e["id"] for e in client.get("/api/knowledge").json()["entries"]] == [entry["id"]]
    comment = report["comments"][0]
    assert client.post("/api/knowledge", json={"video_id": VID, "item_id": comment["id"]}).status_code == 200


def test_mark_known_rejects_unknown_items_and_videos(client):
    seed()
    assert client.post("/api/knowledge", json={"video_id": VID, "item_id": "nope"}).status_code == 404
    assert client.post("/api/knowledge", json={"video_id": "AAAAAAAAAAA", "item_id": "x"}).status_code == 404
    assert client.post("/api/knowledge", json={"video_id": "../../etc", "item_id": "x"}).status_code == 404
    assert client.get("/api/knowledge").json() == {"entries": []}


def test_forget_known_removes_the_entry(client):
    report = seed()
    entry = client.post("/api/knowledge", json={"video_id": VID, "item_id": report["summary"]["key_points"][0]["id"]}).json()
    assert client.delete(f"/api/knowledge/{entry['id']}").json() == {"removed": entry["id"]}
    assert client.delete(f"/api/knowledge/{entry['id']}").status_code == 404
    assert client.delete("/api/knowledge/not-an-id").status_code == 404
    assert client.get("/api/knowledge").json() == {"entries": []}


# ---- personalization on delivery -----------------------------------------------------------------
def test_cached_report_is_personalized_with_what_the_user_marked(client):
    report = seed()
    kp = report["summary"]["key_points"][0]

    async def fake_personalizer(rep):
        return {kp["id"]: {"known": True, "entry_id": "abc1234567", "concept": "A speed"}}

    app.dependency_overrides[get_personalizer] = lambda: fake_personalizer
    with client.stream("GET", "/api/analyze/stream", params={"video_url": URL}) as r:
        events = parse_sse("".join(r.iter_text()))
    assert [e for e, _ in events] == ["cached", "report", "done"]
    html = dict(events)["report"]
    assert "You already know 1 of these points" in html and "3 new to you" in html


def test_default_personalizer_does_nothing_until_the_user_marks_something(client):
    seed()  # empty knowledge store: the default personalizer must not call any model
    with client.stream("GET", "/api/analyze/stream", params={"video_url": URL}) as r:
        html = dict(parse_sse("".join(r.iter_text())))["report"]
    assert "You already know" not in html and "4 ideas" in html


def test_fresh_run_report_is_personalized_too(client):
    report = sample_report()
    kp = report["summary"]["key_points"][1]

    async def runner(url):
        yield {"type": "final", "state": {"video_id": VID, "final_report_md": "# md", "report": report}}

    async def fake_personalizer(rep):
        return {kp["id"]: {"known": True, "entry_id": "abc1234567", "concept": "x"}}

    app.dependency_overrides[get_runner] = lambda: runner
    app.dependency_overrides[get_personalizer] = lambda: fake_personalizer
    with client.stream("GET", "/api/analyze/stream", params={"video_url": URL, "refresh": "true"}) as r:
        html = dict(parse_sse("".join(r.iter_text())))["report"]
    assert "You already know 1 of these points" in html


def test_post_analyze_returns_the_structured_report_and_known_map(client):
    report = seed()
    kp = report["summary"]["key_points"][0]

    async def fake_personalizer(rep):
        return {kp["id"]: {"known": True, "entry_id": "abc1234567", "concept": "x"}}

    app.dependency_overrides[get_personalizer] = lambda: fake_personalizer
    body = client.post("/api/analyze", json={"video_url": URL}).json()
    assert body["cached"] is True and body["report"]["version"] == 2 and kp["id"] in body["known"]


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

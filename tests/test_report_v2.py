"""Report v3: scoring, guardrails, structure, markdown (no network)."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from agent.nodes import claim_cap, format_comments, topic_cap
from agent.report import (
    build_report,
    clean_outside_discussion,
    compute_accuracy_score,
    percent_correct,
    reliability,
    render_markdown,
)


def result(verdict, importance="medium", claim="c", confidence="high", sources=("https://a.example",)):
    return {
        "timestamp": "01:00", "claim": claim, "verdict": verdict, "evidence": "e", "sources": list(sources),
        "confidence": confidence, "importance": importance,
    }


def test_score_is_weighted_by_importance_and_ignores_unverified():
    assert compute_accuracy_score([result("Verified", "high"), result("Debunked", "low")]) == 75
    assert compute_accuracy_score([result("Verified", "low"), result("Debunked", "high")]) == 25
    assert compute_accuracy_score([result("Unverified", "high")]) == 0


def test_reliability_counts():
    r = reliability([result("Verified"), result("Mostly True"), result("Exaggerated"), result("Debunked"), result("Partially False"), result("Unverified")])
    assert r["checked"] == 5 and r["hold_up"] == 2 and r["contradicted"] == 2 and r["exaggerated"] == 1 and r["unverified"] == 1


def test_outside_discussion_drops_tool_meta_and_urlless_items():
    good = {"category": "Correction", "source": "Hacker News", "url": "https://news.ycombinator.com/item?id=1", "detail": "Benchmarks show 1.4x."}
    items = [
        good,
        {**good, "detail": "Benchmarks show 1.4x."},  # duplicate
        {"category": "Other", "source": "Historical claims database", "url": "https://x.example", "detail": "Lookups returned nothing."},
        {"category": "Other", "source": "Social reactions", "url": "https://x.example", "detail": "No community discussion found."},
        {"category": "Other", "source": "Blog", "url": "", "detail": "No url."},
        {"category": "Other", "source": "Blog", "url": "javascript:alert(1)", "detail": "Bad url."},
    ]
    assert clean_outside_discussion(items) == [good]


@pytest.mark.parametrize("last_start,expected", [(0, 3), (120, 3), (900, 5), (1200, 7), (1500, 8), (7200, 8)])
def test_claim_cap_scales_with_video_length(last_start, expected):
    assert claim_cap([{"start_sec": last_start}]) == expected
    assert claim_cap([]) == 3


def state_for_report() -> Dict[str, Any]:
    return {
        "video_id": "dQw4w9WgXcQ",
        "metadata": {"title": "T", "channel_name": "Chan", "links": [{"affiliate": True}, {"affiliate": False}], "promo_codes": ["ABC"]},
        "channel_history": {"total_videos_analyzed": 2, "avg_accuracy_score": 70.0, "frequent_sponsors": ["NordVPN"]},
        "extracted_claims": [{"claim": "A is 4x", "timestamp": "01:00", "importance": "high"}, {"claim": "B costs $5", "timestamp": "02:00", "importance": "low"}],
        "transcript_summary": {
            "video_type": "review", "tldr": "Summary.", "topic_tags": ["x"],
            "clickbait": {"verdict": "v", "payoff_timestamp": "08:14", "clickbait_rating": "Low"},
            "key_points": [
                {"timestamp": "01:00", "point": "A is 4x faster.", "concept": "A speed", "importance": "high", "claim_index": 0},
                {"timestamp": "02:00", "point": "B costs five dollars.", "concept": "B price", "importance": "low", "claim_index": 1},
                {"timestamp": "03:00", "point": "General idea.", "concept": "general idea", "importance": "medium", "claim_index": None},
                {"timestamp": "03:30", "point": "Bad index.", "concept": "bad", "importance": "low", "claim_index": 9},
            ],
            "sponsors": [{"name": "NordVPN", "timestamp_range": "", "disclosed": False}], "has_paid_promotion": True, "bias_notes": "bias",
            "extra_sections": [{"title": "Steps", "kind": "steps", "items": [{"label": "1", "text": "do it"}]}],
        },
        "topics": [
            {"title": "Speed claims", "timestamp": "01:00", "summary": "The host says A is 4x faster; benchmarks show 1.4x.", "source_urls": ["https://a.example"]},
            {"title": "Pricing", "timestamp": "02:00", "summary": "B costs five dollars.", "source_urls": []},
        ],
        "comment_insights": [{"category": "Correction", "author": "@u", "likes": 9, "concept": "A is not 4x", "detail": "It is 1.4x."}],
        "community_insights": [{"category": "Correction", "source": "HN", "url": "https://hn.example/1", "detail": "Seen on HN."}],
    }


def test_build_report_links_key_points_to_checks_and_orders_corrections():
    results = [result("Debunked", "high", claim="A is 4x"), result("Verified", "low", claim="B costs $5")]
    report = build_report(state_for_report(), results, compute_accuracy_score(results))

    kps = report["summary"]["key_points"]
    assert kps[0]["check"] == {"verdict": "Debunked", "confidence": "high"}
    assert kps[1]["check"]["verdict"] == "Verified"
    assert kps[2]["check"] is None and kps[3]["check"] is None  # unlinked and out-of-range indexes are not checked
    assert [c["claim"] for c in report["corrections"]] == ["A is 4x"] and report["unverified"] == []
    assert report["reliability"]["hold_up"] == 1 and report["video_type"] == "review"
    assert report["comments"][0]["detail"] == "It is 1.4x." and report["outside_discussion"][0]["url"] == "https://hn.example/1"
    assert report["before_you_watch"]["affiliate_links"] == 1 and "70.0% average" in report["before_you_watch"]["channel_record"]




def test_markdown_has_summary_topics_fact_table_and_before_you_watch():
    results = [result("Debunked", "high", claim="A is 4x"), result("Unverified", "medium", claim="B costs $5", sources=())]
    md = render_markdown(build_report(state_for_report(), results, 0))
    for heading in ("## Summary", "### Speed claims", "## Fact check", "## Before you watch"):
        assert heading in md
    assert "| Time | Claim | Verdict | Sources |" in md and "❌ False" in md and "❔ Unverified" in md
    assert "**Clickbait:** No" in md and "**Paid promotion**" in md
    assert "What you'll learn" not in md and "From the comments" not in md


def test_markdown_falls_back_to_key_ideas_when_there_are_no_topics_and_says_so_when_nothing_was_checked():
    state = state_for_report()
    state["topics"] = []
    report = build_report(state, [], 0)
    md = render_markdown(report)
    assert "### A speed" in md and "No claims were checked." in md and "no facts could be checked" in md


def test_percent_correct_is_weighted_and_none_without_checks():
    assert percent_correct([result("Verified", "high"), result("Debunked", "low")]) == 75
    assert percent_correct([result("Mostly True", "low"), result("Exaggerated", "high")]) == 25
    assert percent_correct([result("Unverified", "high")]) is None and percent_correct([]) is None
    assert reliability([result("Verified"), result("Debunked")])["percent_correct"] == 50


def test_fact_check_rows_use_plain_labels_and_follow_video_order():
    results = [
        {**result("Verified", claim="late"), "timestamp": "09:00"},
        {**result("Partially False", claim="early"), "timestamp": "01:00"},
        {**result("Debunked", claim="mid"), "timestamp": "05:00"},
        result("Unverified", claim="none", sources=()),
    ]
    rows = build_report(state_for_report(), results, 0)["fact_check"]
    assert [(r["claim"], r["rating"]) for r in rows] == [("early", "Doubtful"), ("none", "Unverified"), ("mid", "False"), ("late", "Correct")]


def test_format_comments_nests_best_replies_collapses_whitespace_and_respects_the_budget(monkeypatch):
    thread = {"author": "@a", "likes": 7, "text": "line one\n\nline two", "is_creator": False, "replies": [
        {"author": f"@r{i}", "likes": i, "text": f"reply {i}", "is_creator": i == 4} for i in range(5)
    ]}
    block = format_comments([thread])[0].splitlines()
    assert block[0] == "- @a [7 likes]: line one line two"
    assert block[1:] == ["  > @r4 (CREATOR) [4 likes]: reply 4", "  > @r3 [3 likes]: reply 3", "  > @r2 [2 likes]: reply 2"]
    assert format_comments([{"author": "@x", "likes": 1, "text": "z" * 900}])[0].endswith("[truncated 500 chars]")
    import agent.nodes as nodes

    monkeypatch.setattr(nodes, "MAX_COMMENT_CHARS", 60)
    many = [{"author": "@x", "likes": 1, "text": "y" * 30, "replies": []} for _ in range(10)]
    assert 1 <= len(format_comments(many)) < 10  # the budget cuts the tail, never the first (best) comments


@pytest.mark.parametrize("seconds,expected", [(0, 3), (300, 3), (900, 7), (1800, 10), (7200, 10)])
def test_topic_cap_scales_with_video_length(seconds, expected):
    assert topic_cap(seconds) == expected

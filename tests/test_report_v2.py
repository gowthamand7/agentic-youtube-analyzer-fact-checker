"""Report v2: scoring, guardrails, structure, markdown (no network)."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from agent.nodes import claim_cap
from agent.report import (
    build_report,
    clean_outside_discussion,
    compute_accuracy_score,
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


def test_markdown_has_the_v2_sections_and_hides_empty_ones():
    results = [result("Debunked", "high", claim="A is 4x"), result("Unverified", "medium", claim="B costs $5", sources=())]
    md = render_markdown(build_report(state_for_report(), results, 0))
    for heading in ("## What you'll learn", "## What's wrong or doubtful", "## From the comments", "## Elsewhere on the web", "## Steps", "## Before you watch"):
        assert heading in md
    assert "*Couldn't verify:* B costs $5" in md and "1. **1** do it" in md

    state = state_for_report()
    state.update(comment_insights=[], community_insights=[])
    state["transcript_summary"]["sponsors"], state["transcript_summary"]["extra_sections"] = [], []
    md = render_markdown(build_report(state, [result("Verified", claim="A is 4x")], 100))
    assert "Elsewhere on the web" not in md and "No comments added new information." in md
    assert "Nothing in the checked claims was contradicted." in md

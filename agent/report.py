"""Deterministic scoring, evidence guardrails and Markdown rendering."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

VERDICT_SCORES = {
    "Verified": 100,
    "Mostly True": 85,
    "Exaggerated": 50,
    "Partially False": 35,
    "Debunked": 0,
}
VERDICT_ICONS = {
    "Verified": "✅",
    "Mostly True": "✅",
    "Exaggerated": "⚠️",
    "Partially False": "⚠️",
    "Debunked": "❌",
    "Unverified": "❔",
}
ADDITION_ICONS = {
    "Tutorial Fix": "🔧",
    "Free Alternative": "💸",
    "Reliability Report": "⏳",
    "Correction": "📝",
    "Other": "💬",
}
TRANSCRIPT_ERROR = "Transcripts are disabled or unavailable for this video; cannot perform spoken claim auditing."


def timestamp_to_seconds(ts: str) -> Optional[int]:
    m = re.fullmatch(r"\s*(?:(\d+):)?(\d{1,2}):(\d{2})\s*", ts or "")
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(s)


def ts_link(video_id: str, ts: str) -> str:
    secs = timestamp_to_seconds(ts)
    if secs is None:
        return ts or "—"
    return f"[{ts}](https://youtube.com/watch?v={video_id}&t={secs}s)"


def enforce_evidence(result: Dict[str, Any]) -> Dict[str, Any]:
    """Anti-hallucination guard: a non-Unverified verdict requires at least one cited source."""
    out = dict(result)
    sources = [s for s in out.get("sources", []) if isinstance(s, str) and s.startswith(("http://", "https://"))]
    out["sources"] = sources
    if out.get("verdict") != "Unverified" and not sources:
        out["verdict"] = "Unverified"
        out["confidence"] = "low"
        out["evidence"] = "Lacks evidence: no retrieved source supports a verdict. " + out.get("evidence", "")
    return out


def compute_accuracy_score(results: List[Dict[str, Any]]) -> int:
    """Mean verdict score over checked claims (Unverified claims are excluded); 0 when nothing could be checked."""
    scored = [VERDICT_SCORES[r["verdict"]] for r in results if r.get("verdict") in VERDICT_SCORES]
    return round(sum(scored) / len(scored)) if scored else 0


def _trend(score: int, history: Dict[str, Any]) -> str:
    total = history.get("total_videos_analyzed", 0) or 0
    if not total:
        return "first analyzed video"
    delta = round(score - float(history.get("avg_accuracy_score", 0)))
    arrow = "▲" if delta > 0 else "▼" if delta < 0 else "▬"
    return f"Trending {arrow} {delta:+d}% vs. prior average"


def build_analysis_payload(state: Dict[str, Any], results: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary = state.get("transcript_summary", {})
    clickbait = summary.get("clickbait", {})
    links = state.get("metadata", {}).get("links", [])
    sponsors = summary.get("sponsors", [])
    return {
        "clickbait_buster": {
            "verdict": clickbait.get("verdict", ""),
            "payoff_timestamp": clickbait.get("payoff_timestamp", ""),
            "clickbait_rating": clickbait.get("clickbait_rating", ""),
        },
        "paid_promotion_details": {
            "affiliate_links_count": sum(1 for l in links if l.get("affiliate")),
            "sponsor_segment_timestamps": [s["timestamp_range"] for s in sponsors if s.get("timestamp_range")],
            "bias_notes": summary.get("bias_notes", ""),
        },
        "claims_verified": [
            {k: r.get(k) for k in ("timestamp", "claim", "verdict", "evidence", "sources")} for r in results
        ],
        "crowdsourced_additions": state.get("community_insights", []) + summary.get("crowdsourced_additions", []),
    }


def build_save_payload(state: Dict[str, Any], results: List[Dict[str, Any]], score: int) -> Dict[str, Any]:
    meta = state["metadata"]
    summary = state.get("transcript_summary", {})
    return {
        "channel_id": meta["channel_id"],
        "channel_name": meta.get("channel_name", ""),
        "video_id": state["video_id"],
        "video_title": meta.get("title", ""),
        "overall_accuracy_score": score,
        "has_paid_promotion": summary.get("has_paid_promotion", False),
        "disclosed_properly": summary.get("disclosed_properly", False),
        "sponsors": [s["name"] for s in summary.get("sponsors", [])],
        "collaborators": summary.get("collaborators", []),
        "topic_tags": summary.get("topic_tags", []),
        "analysis_payload": build_analysis_payload(state, results),
    }


def _cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def render_error_report(message: str, state: Dict[str, Any]) -> str:
    title = state.get("metadata", {}).get("title")
    head = f"# ⛔ Analysis Aborted\n\n**{message}**\n"
    if title:
        head += f"\nVideo: *{title}*\n"
    return head + "\nNo claims were audited and nothing was saved to the channel history.\n"


def render_report(state: Dict[str, Any], results: List[Dict[str, Any]], score: int) -> str:
    vid = state["video_id"]
    meta = state.get("metadata", {})
    summary = state.get("transcript_summary", {})
    clickbait = summary.get("clickbait", {})
    history = state.get("channel_history", {}) or {}
    links = meta.get("links", [])
    sponsors = summary.get("sponsors", [])
    frequent = {s.lower() for s in history.get("frequent_sponsors", []) or []}

    lines = [
        f"# 🎯 Clickbait Buster — {meta.get('title', '')}",
        f"**TL;DW Verdict:** {clickbait.get('verdict', 'n/a')}  ",
        f"**Jump to the Point:** {ts_link(vid, clickbait.get('payoff_timestamp', ''))} | "
        f"**Clickbait Rating:** {clickbait.get('clickbait_rating', 'n/a')}",
        "",
        "---",
        "",
        "## 🛡️ Creator Context & Sponsorship Alert",
    ]
    total = history.get("total_videos_analyzed", 0) or 0
    if total:
        lines.append(
            f"* **Channel Track Record:** {history.get('avg_accuracy_score', 0)}% avg accuracy across {total} "
            f"previously analyzed videos ({_trend(score, history)})"
        )
    else:
        lines.append(
            f"* **Channel Track Record:** No prior history for *{meta.get('channel_name', 'this channel')}* "
            f"({_trend(score, history)})"
        )
    if sponsors:
        parts = []
        for s in sponsors:
            tag = " — *Recurring sponsor*" if s["name"].lower() in frequent else ""
            when = f" ({ts_link(vid, s['timestamp_range'].split('-')[0].strip())})" if s.get("timestamp_range") else ""
            disc = "" if s.get("disclosed") else " — **not clearly disclosed**"
            parts.append(f"{s['name']}{when}{tag}{disc}")
        lines.append("* **Detected Sponsors:** " + "; ".join(parts))
    else:
        lines.append("* **Detected Sponsors:** none detected")
    affiliates = sum(1 for l in links if l.get("affiliate"))
    codes = meta.get("promo_codes", [])
    lines.append(
        f"* **Affiliate Links Found:** {affiliates} of {len(links)} description links"
        + (f"; promo codes: {', '.join(codes)}" if codes else "")
    )
    if summary.get("bias_notes"):
        lines.append(f"* **Bias Warning:** {summary['bias_notes']}")

    lines += ["", "---", "", f"## ⚖️ Fact-Check Scorecard (Overall Accuracy: {score}/100)"]
    if results:
        lines += ["| Timestamp | Host's Claim | Verdict | Verification & Evidence |", "| :--- | :--- | :--- | :--- |"]
        for r in results:
            srcs = " ".join(f"[{i + 1}]({u})" for i, u in enumerate(r.get("sources", [])))
            lines.append(
                f"| {ts_link(vid, r.get('timestamp', ''))} | {_cell(r['claim'])} | "
                f"{VERDICT_ICONS.get(r['verdict'], '')} **{r['verdict']}** | {_cell(r.get('evidence', ''))} {srcs} |"
            )
    else:
        lines.append("No verifiable claims were extracted.")

    additions = state.get("community_insights", []) + summary.get("crowdsourced_additions", [])
    lines += ["", "---", "", "## 💡 Crowdsourced Additions"]
    if additions:
        for a in additions:
            lines.append(f"* **{ADDITION_ICONS.get(a['category'], '💬')} {a['category']} ({a['source']}):** {a['detail']}")
    else:
        lines.append("* No notable community additions found.")

    lines += ["", "---", "", "## 📋 Actionable Takeaways & Mentioned Resources"]
    lines += [f"* [ ] {t}" for t in summary.get("takeaways", [])] or ["* None extracted."]
    if summary.get("resources"):
        lines.append("* **Links & Tools Mentioned:** " + ", ".join(summary["resources"]))
    return "\n".join(lines) + "\n"

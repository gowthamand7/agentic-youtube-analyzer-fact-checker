"""Report v2: structured report builder, scoring, evidence guardrails, and the Markdown renderer.

The structured dict from `build_report` is the single source of truth. The web UI renders it to HTML
(app/render.py); `render_markdown` serves the CLI and JSON API.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

IMPORTANCE_WEIGHTS = {"high": 3, "medium": 2, "low": 1}
VERDICT_SCORES = {"Verified": 100, "Mostly True": 85, "Exaggerated": 50, "Partially False": 35, "Debunked": 0}
HOLDS_UP = ("Verified", "Mostly True")
WRONG = ("Exaggerated", "Partially False", "Debunked")
VERDICT_ICONS = {
    "Verified": "✅", "Mostly True": "✅", "Exaggerated": "⚠️", "Partially False": "⚠️", "Debunked": "❌", "Unverified": "❔",
}
ADDITION_ICONS = {
    "Tutorial Fix": "🔧", "Free Alternative": "💸", "Reliability Report": "⏳", "Correction": "📝", "Extra Context": "🧩", "Other": "💬",
}
TRANSCRIPT_ERROR = "Transcripts are disabled or unavailable for this video; cannot perform spoken claim auditing."

# Tool-meta text the model sometimes files as "community discussion"; never real community content.
_JUNK_OUTSIDE = re.compile(
    r"historical claims|social reactions|no (community )?discussion|no results|web search\b|tool (returned|call)|not (find|found)",
    re.IGNORECASE,
)


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


def clean_outside_discussion(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only real outside discussion: a genuine URL and no tool-meta text."""
    seen, kept = set(), []
    for it in items:
        url = it.get("url", "")
        text = f"{it.get('source', '')} {it.get('detail', '')}"
        if not url.startswith(("http://", "https://")) or _JUNK_OUTSIDE.search(text) or it.get("detail") in seen:
            continue
        seen.add(it["detail"])
        kept.append(it)
    return kept


def compute_accuracy_score(results: List[Dict[str, Any]]) -> int:
    """Importance-weighted mean verdict score over checked claims (Unverified excluded); 0 when nothing was checked."""
    total = weight_sum = 0
    for r in results:
        if r.get("verdict") in VERDICT_SCORES:
            w = IMPORTANCE_WEIGHTS.get(r.get("importance", "medium"), 2)
            total += VERDICT_SCORES[r["verdict"]] * w
            weight_sum += w
    return round(total / weight_sum) if weight_sum else 0


def reliability(results: List[Dict[str, Any]]) -> Dict[str, int]:
    verdicts = [r.get("verdict") for r in results]
    right = sum(v in HOLDS_UP for v in verdicts)
    wrong = sum(v in WRONG for v in verdicts)
    return {
        "checked": right + wrong,
        "hold_up": right,
        "contradicted": sum(v in ("Partially False", "Debunked") for v in verdicts),
        "exaggerated": sum(v == "Exaggerated" for v in verdicts),
        "unverified": sum(v == "Unverified" for v in verdicts),
        "score": compute_accuracy_score(results),
    }


def _trend(score: int, history: Dict[str, Any]) -> Optional[str]:
    total = history.get("total_videos_analyzed", 0) or 0
    if not total:
        return None
    delta = round(score - float(history.get("avg_accuracy_score", 0)))
    return f"{history.get('avg_accuracy_score', 0)}% average across {total} earlier video{'s' if total != 1 else ''}; this one {delta:+d}"


def build_report(state: Dict[str, Any], results: List[Dict[str, Any]], score: int) -> Dict[str, Any]:
    """Assemble the structured v2 report from the final agent state."""
    vid = state["video_id"]
    meta = state.get("metadata", {})
    summary = state.get("transcript_summary", {})
    history = state.get("channel_history", {}) or {}
    by_claim = {r["claim"]: r for r in results}
    claims = state.get("extracted_claims", [])

    key_points = []
    def _when(kp: Dict[str, Any]) -> float:
        secs = timestamp_to_seconds(kp["timestamp"])
        return float("inf") if secs is None else secs  # unparseable times go last; 00:00 is a real time

    ordered = sorted(summary.get("key_points", []), key=_when)  # video order
    for kp in ordered:
        idx = kp.get("claim_index")
        res = by_claim.get(claims[idx]["claim"]) if isinstance(idx, int) and 0 <= idx < len(claims) else None
        key_points.append({
            "timestamp": kp["timestamp"], "point": kp["point"], "concept": kp["concept"], "importance": kp["importance"],
            "check": {"verdict": res["verdict"], "confidence": res["confidence"]} if res else None,
        })

    importance_rank = {"high": 0, "medium": 1, "low": 2}
    corrections = sorted((r for r in results if r["verdict"] in WRONG), key=lambda r: importance_rank.get(r.get("importance", "medium"), 1))
    comments = [
        {k: c[k] for k in ("category", "author", "likes", "concept", "detail")}
        for c in state.get("comment_insights", [])
    ]
    outside = [dict(o) for o in clean_outside_discussion(state.get("community_insights", []))]
    links = meta.get("links", [])

    return {
        "version": 2,
        "video_id": vid,
        "title": meta.get("title", ""),
        "channel": meta.get("channel_name", ""),
        "video_type": summary.get("video_type", "other"),
        "topic_tags": summary.get("topic_tags", []),
        "headline": summary.get("clickbait", {}),
        "summary": {"tldr": summary.get("tldr", ""), "key_points": key_points},
        "corrections": corrections,
        "unverified": [r for r in results if r["verdict"] == "Unverified"],
        "checked": results,
        "reliability": reliability(results),
        "comments": comments,
        "outside_discussion": outside,
        "extras": summary.get("extra_sections", []),
        "before_you_watch": {
            "sponsors": summary.get("sponsors", []),
            "has_paid_promotion": summary.get("has_paid_promotion", False),
            "affiliate_links": sum(1 for l in links if l.get("affiliate")),
            "total_links": len(links),
            "promo_codes": meta.get("promo_codes", []),
            "bias_notes": summary.get("bias_notes", ""),
            "frequent_sponsors": history.get("frequent_sponsors", []),
            "channel_record": _trend(score, history),
        },
    }


def build_save_payload(state: Dict[str, Any], results: List[Dict[str, Any]], score: int, report: Dict[str, Any]) -> Dict[str, Any]:
    """Payload for memory-db `save_video_analysis` (keeps the original analysis_payload contract, plus the summary)."""
    meta = state["metadata"]
    summary = state.get("transcript_summary", {})
    links = meta.get("links", [])
    sponsors = summary.get("sponsors", [])
    return {
        "channel_id": meta["channel_id"],
        "channel_name": meta.get("channel_name", ""),
        "video_id": state["video_id"],
        "video_title": meta.get("title", ""),
        "overall_accuracy_score": score,
        "has_paid_promotion": summary.get("has_paid_promotion", False),
        "disclosed_properly": summary.get("disclosed_properly", False),
        "sponsors": [s["name"] for s in sponsors],
        "collaborators": summary.get("collaborators", []),
        "topic_tags": summary.get("topic_tags", []),
        "analysis_payload": {
            "clickbait_buster": report["headline"],
            "paid_promotion_details": {
                "affiliate_links_count": sum(1 for l in links if l.get("affiliate")),
                "sponsor_segment_timestamps": [s["timestamp_range"] for s in sponsors if s.get("timestamp_range")],
                "bias_notes": summary.get("bias_notes", ""),
            },
            "summary": report["summary"]["tldr"],
            "claims_verified": [
                {k: r.get(k) for k in ("timestamp", "claim", "verdict", "evidence", "sources")} for r in results
            ],
            "crowdsourced_additions": report["comments"] + report["outside_discussion"],
        },
    }


# ---- Markdown (CLI / JSON API) ------------------------------------------------------------------
def _cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def render_error_report(message: str, state: Dict[str, Any]) -> str:
    title = state.get("metadata", {}).get("title")
    head = f"# ⛔ Analysis Aborted\n\n**{message}**\n"
    if title:
        head += f"\nVideo: *{title}*\n"
    return head + "\nNo claims were audited and nothing was saved to the channel history.\n"


def render_markdown(report: Dict[str, Any]) -> str:
    vid = report["video_id"]
    hl, rel, byw = report["headline"], report["reliability"], report["before_you_watch"]
    lines = [
        f"# {report['title']}",
        f"*{report['channel']} · {report['video_type']}*",
        "",
        f"**TL;DW:** {report['summary']['tldr']}",
        "",
        f"**Title check:** {hl.get('verdict', 'n/a')} (answer at {ts_link(vid, hl.get('payoff_timestamp', ''))}; "
        f"clickbait: {hl.get('clickbait_rating', 'n/a')})",
    ]
    if rel["checked"] or rel["unverified"]:
        parts = [f"{rel['hold_up']} of {rel['checked']} checked claims hold up"] if rel["checked"] else ["no claims could be checked"]
        if rel["contradicted"]:
            parts.append(f"{rel['contradicted']} contradicted")
        if rel["exaggerated"]:
            parts.append(f"{rel['exaggerated']} exaggerated")
        if rel["unverified"]:
            parts.append(f"{rel['unverified']} couldn't be verified")
        lines.append(f"**Reliability:** {', '.join(parts)} (weighted score {rel['score']}/100)")

    lines += ["", "## What you'll learn"]
    for kp in report["summary"]["key_points"]:
        badge = ""
        if kp["check"]:
            badge = f" {VERDICT_ICONS.get(kp['check']['verdict'], '')} *{kp['check']['verdict']}*"
        lines.append(f"* [{kp['timestamp']}] {kp['point']}{badge}")

    lines += ["", "## What's wrong or doubtful"]
    if report["corrections"]:
        lines += ["| Timestamp | Claim | Verdict | Evidence |", "| :--- | :--- | :--- | :--- |"]
        for r in report["corrections"]:
            srcs = " ".join(f"[{i + 1}]({u})" for i, u in enumerate(r.get("sources", [])))
            lines.append(
                f"| {ts_link(vid, r['timestamp'])} | {_cell(r['claim'])} | {VERDICT_ICONS[r['verdict']]} **{r['verdict']}** | "
                f"{_cell(r.get('evidence', ''))} {srcs} |"
            )
    else:
        lines.append("Nothing in the checked claims was contradicted.")
    if report["unverified"]:
        lines.append("\n*Couldn't verify:* " + "; ".join(r["claim"] for r in report["unverified"]))

    lines += ["", "## From the comments"]
    lines += [
        f"* {ADDITION_ICONS.get(c['category'], '💬')} **{c['category']}** ({c['author']}, {c['likes']} likes): {c['detail']}"
        for c in report["comments"]
    ] or ["* No comments added new information."]
    if report["outside_discussion"]:
        lines += ["", "## Elsewhere on the web"]
        lines += [f"* {ADDITION_ICONS.get(o['category'], '💬')} **{o['category']}** ([{o['source']}]({o['url']})): {o['detail']}" for o in report["outside_discussion"]]

    for sec in report["extras"]:
        lines += ["", f"## {sec['title']}"]
        for i, it in enumerate(sec["items"], 1):
            label = f"**{it['label']}** " if it.get("label") else ""
            lines.append(f"{i}. {label}{it['text']}" if sec["kind"] == "steps" else f"* {label}{it['text']}")

    notes = []
    if byw["sponsors"]:
        notes.append("Sponsors: " + "; ".join(f"{s['name']}{'' if s.get('disclosed') else ' (not clearly disclosed)'}" for s in byw["sponsors"]))
    if byw["affiliate_links"]:
        notes.append(f"{byw['affiliate_links']} of {byw['total_links']} description links are affiliate links")
    if byw["promo_codes"]:
        notes.append("Promo codes: " + ", ".join(byw["promo_codes"]))
    if byw["bias_notes"]:
        notes.append(byw["bias_notes"])
    if byw["channel_record"]:
        notes.append("Channel record: " + byw["channel_record"])
    if notes:
        lines += ["", "## Before you watch"] + [f"* {n}" for n in notes]
    return "\n".join(lines) + "\n"

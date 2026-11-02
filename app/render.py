"""Render a structured report (agent/report.py) to sanitized HTML.

Every dynamic value goes through html.escape; links are limited to https://youtube.com timestamps and http(s) sources.
"""

from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Optional

from agent.report import ADDITION_ICONS, VERDICT_ICONS, timestamp_to_seconds

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_VERDICT_CLASS = {
    "Verified": "good", "Mostly True": "good", "Exaggerated": "warn", "Partially False": "warn", "Debunked": "bad", "Unverified": "none",
}


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def ts_link(video_id: str, ts: str) -> str:
    secs = timestamp_to_seconds(ts)
    if secs is None or not _VIDEO_ID.fullmatch(video_id):
        return esc(ts)
    return f'<a href="https://youtube.com/watch?v={video_id}&amp;t={secs}s" target="_blank" rel="noopener">{esc(ts)}</a>'


def _safe_url(url: str) -> Optional[str]:
    return url if isinstance(url, str) and url.startswith(("http://", "https://")) else None


def _source_links(sources: List[str]) -> str:
    links = "".join(
        f'<li><a href="{esc(u)}" target="_blank" rel="noopener noreferrer">{esc(u)}</a></li>' for u in sources if _safe_url(u)
    )
    return f'<ul class="sources">{links}</ul>' if links else ""


def _check_badge(check: Optional[Dict[str, str]]) -> str:
    if not check:
        return ""
    cls = _VERDICT_CLASS.get(check["verdict"], "none")
    label = {"good": "checked", "warn": "disputed", "bad": "contradicted", "none": "unchecked"}[cls]
    return f'<span class="verdict verdict-{cls}" title="{esc(check["verdict"])} ({esc(check["confidence"])} confidence)">{VERDICT_ICONS.get(check["verdict"], "")} {label}</span>'


def _section(title: str, body: str, count: Optional[str] = None, cls: str = "") -> str:
    badge = f' <span class="count">{esc(count)}</span>' if count else ""
    return f'<section class="r-sec {cls}"><h3>{esc(title)}{badge}</h3>{body}</section>'


def _claim_card(vid: str, r: Dict[str, Any], open_: bool = False) -> str:
    cls = _VERDICT_CLASS.get(r["verdict"], "none")
    return (
        f'<details class="claim claim-{cls}"{" open" if open_ else ""}><summary>'
        f'<span class="verdict verdict-{cls}">{esc(r["verdict"])}</span><span class="claim-text">{esc(r["claim"])}</span></summary>'
        f'<div class="claim-body"><p class="meta">At {ts_link(vid, r["timestamp"])} · confidence: {esc(r.get("confidence", "n/a"))}'
        f' · importance: {esc(r.get("importance", "medium"))}</p><p>{esc(r.get("evidence", ""))}</p>{_source_links(r.get("sources", []))}</div></details>'
    )


def render_report_html(report: Dict[str, Any], aborted: bool = False) -> str:
    vid = report["video_id"]
    rel, hl, byw = report["reliability"], report["headline"], report["before_you_watch"]
    parts: List[str] = []

    # ---- header: summary + at-a-glance verdicts ----
    chips = [f'<span class="chip">{esc(report["video_type"])}</span>']
    if hl.get("clickbait_rating"):
        chips.append(f'<span class="chip">Clickbait: {esc(hl["clickbait_rating"])}</span>')
    if byw["has_paid_promotion"]:
        names = ", ".join(s["name"] for s in byw["sponsors"])
        chips.append(f'<span class="chip warn">Paid promotion{": " + esc(names) if names else ""}</span>')
    if rel["checked"]:
        chips.append(f'<span class="chip {"good" if rel["hold_up"] == rel["checked"] else "warn"}">{rel["hold_up"]} of {rel["checked"]} checked claims hold up</span>')
    if rel["contradicted"] or rel["exaggerated"]:
        chips.append(f'<span class="chip bad">{rel["contradicted"] + rel["exaggerated"]} wrong or exaggerated</span>')
    if rel["unverified"]:
        chips.append(f'<span class="chip muted">{rel["unverified"]} unverified</span>')
    title_check = ""
    if hl.get("verdict"):
        title_check = f'<p class="title-check"><strong>Title check:</strong> {esc(hl["verdict"])} <span class="meta">(answer at {ts_link(vid, hl.get("payoff_timestamp", ""))})</span></p>'
    parts.append(
        f'<div class="r-head"><h2>{esc(report["title"])}</h2><div class="chips">{"".join(chips)}</div>'
        f'<p class="tldr">{esc(report["summary"]["tldr"])}</p>{title_check}</div>'
    )

    # ---- what you'll learn ----
    points = report["summary"]["key_points"]

    def point_html(kp: Dict[str, Any]) -> str:
        key = '<span class="chip key">key idea</span>' if kp["importance"] == "high" else ""
        return f'<li class="item"><span class="item-main"><span class="ts">{ts_link(vid, kp["timestamp"])}</span> {esc(kp["point"])} {key} {_check_badge(kp["check"])}</span></li>'

    parts.append(_section("What you'll learn", f'<ul class="learn">{"".join(point_html(kp) for kp in points)}</ul>', f"{len(points)} ideas", "learn-sec"))

    # ---- what's wrong or doubtful ----
    wrong = "".join(_claim_card(vid, r, open_=i == 0) for i, r in enumerate(report["corrections"]))
    if not wrong:
        wrong = '<p class="empty">Nothing in the checked claims was contradicted or exaggerated.</p>'
    unverified = ""
    if report["unverified"]:
        unverified = (
            '<details class="fold-group"><summary>Couldn\'t verify ' + str(len(report["unverified"])) + "</summary>"
            + "".join(_claim_card(vid, r) for r in report["unverified"]) + "</details>"
        )
    parts.append(_section("What's wrong or doubtful", f'<div class="claims">{wrong}</div>{unverified}', cls="wrong-sec"))

    # ---- from the comments ----
    def comment_html(c: Dict[str, Any]) -> str:
        return (
            f'<li class="item"><span class="item-main"><span class="chip">{ADDITION_ICONS.get(c["category"], "💬")} {esc(c["category"])}</span> '
            f'{esc(c["detail"])} <span class="meta">— {esc(c["author"])}, {esc(c["likes"])} likes</span></span></li>'
        )

    comments = report["comments"]
    body = f'<ul class="learn">{"".join(comment_html(c) for c in comments)}</ul>' if comments else '<p class="empty">No comments added new information.</p>'
    parts.append(_section("From the comments", body))

    # ---- elsewhere on the web ----
    if report["outside_discussion"]:
        items = "".join(
            f'<li class="item"><span class="item-main"><span class="chip">{ADDITION_ICONS.get(o["category"], "💬")} {esc(o["category"])}</span> '
            f'{esc(o["detail"])} <a class="meta" href="{esc(o["url"])}" target="_blank" rel="noopener noreferrer">{esc(o["source"])}</a></span></li>'
            for o in report["outside_discussion"] if _safe_url(o["url"])
        )
        parts.append(_section("Elsewhere on the web", f'<ul class="learn">{items}</ul>'))

    # ---- dynamic sections chosen for this video ----
    for sec in report["extras"]:
        tag = "ol" if sec["kind"] == "steps" else "ul"
        rows = "".join(
            f'<li>{"<strong>" + esc(it["label"]) + "</strong> " if it.get("label") else ""}{esc(it["text"])}</li>' for it in sec["items"]
        )
        parts.append(_section(sec["title"], f'<{tag} class="extra extra-{esc(sec["kind"])}">{rows}</{tag}>', cls=f"extra-sec kind-{esc(sec['kind'])}"))

    # ---- transparency: every checked claim ----
    if report["checked"]:
        parts.append(
            f'<details class="r-sec fold"><summary>All {len(report["checked"])} fact-checked claims</summary>'
            f'<div class="claims">{"".join(_claim_card(vid, r) for r in report["checked"])}</div></details>'
        )

    # ---- before you watch ----
    notes_list = []
    if byw["sponsors"]:
        notes_list.append("Sponsors: " + "; ".join(esc(s["name"]) + ("" if s.get("disclosed") else " (not clearly disclosed)") for s in byw["sponsors"]))
    if byw["affiliate_links"]:
        notes_list.append(f'{byw["affiliate_links"]} of {byw["total_links"]} description links are affiliate links')
    if byw["promo_codes"]:
        notes_list.append("Promo codes: " + ", ".join(esc(c) for c in byw["promo_codes"]))
    if byw["bias_notes"]:
        notes_list.append(esc(byw["bias_notes"]))
    if byw["channel_record"]:
        notes_list.append("Channel record: " + esc(byw["channel_record"]))
    if notes_list:
        parts.append('<details class="r-sec fold"><summary>Before you watch</summary><ul>' + "".join(f"<li>{n}</li>" for n in notes_list) + "</ul></details>")

    flag = ' data-aborted="1"' if aborted else ""
    return f'<article class="report v2" data-video-id="{esc(vid)}"{flag}>{"".join(parts)}</article>'

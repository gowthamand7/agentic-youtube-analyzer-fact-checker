"""Render a structured report (agent/report.py) to sanitized HTML: a Summary tab and a Fact check tab.

Every dynamic value goes through html.escape; links are limited to https://youtube.com timestamps and http(s) sources.
"""

from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from agent.report import FACT_LABEL, percent_correct, timestamp_to_seconds

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_RATING_CLASS = {"Correct": "good", "Doubtful": "warn", "False": "bad", "Unverified": "none"}
_CLICKBAIT = {"Low": ("No", "good"), "Medium": ("Medium", "warn"), "High": ("High", "bad")}


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def ts_link(video_id: str, ts: str) -> str:
    secs = timestamp_to_seconds(ts)
    if secs is None or not _VIDEO_ID.fullmatch(video_id):
        return esc(ts)
    return f'<a href="https://youtube.com/watch?v={video_id}&amp;t={secs}s" target="_blank" rel="noopener">{esc(ts)}</a>'


def _safe_url(url: str) -> Optional[str]:
    return url if isinstance(url, str) and url.startswith(("http://", "https://")) else None


def _domain(url: str) -> str:
    return (urlparse(url).hostname or url).removeprefix("www.")


def _source_links(urls: List[str], cls: str = "sources") -> str:
    links = "".join(
        f'<a href="{esc(u)}" target="_blank" rel="noopener noreferrer" title="{esc(u)}">{esc(_domain(u))}</a>'
        for u in dict.fromkeys(urls)
        if _safe_url(u)
    )
    return f'<span class="{cls}">{links}</span>' if links else ""


def _percent_chip(pct: Optional[int], rel: Dict[str, Any]) -> str:
    if pct is None:
        return '<span class="chip muted">Facts correct: n/a</span>'
    cls = "good" if pct >= 80 else "warn" if pct >= 50 else "bad"
    detail = f"{rel.get('hold_up', 0)} of {rel.get('checked', 0)} checked claims hold up (weighted by importance)"
    if rel.get("unverified"):
        detail += f"; {rel['unverified']} could not be verified and are not counted"
    return f'<span class="chip score {cls}" title="{esc(detail)}">{pct}% of facts correct</span>'


def _sponsor_chip(sponsors: List[Dict[str, Any]]) -> str:
    """'Paid promotion: A, B +1'; the model sometimes puts a description in the name, so keep the text before it."""
    names = list(dict.fromkeys(re.split(r"\s*[(\[:,–—]\s*", s["name"], maxsplit=1)[0].strip() for s in sponsors if s.get("name")))
    full = "; ".join(s["name"] for s in sponsors if s.get("name"))
    shown = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
    return f'<span class="chip warn" title="{esc(full)}">Paid promotion{": " + esc(shown) if shown else ""}</span>'


def _fact_rows(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Table rows; reports saved before v3 have no `fact_check`, so derive it from the checked claims."""
    if "fact_check" in report:
        return report["fact_check"]
    return [
        {
            "timestamp": r["timestamp"], "claim": r["claim"], "rating": FACT_LABEL.get(r["verdict"], "Unverified"),
            "verdict": r["verdict"], "confidence": r.get("confidence", ""), "sources": r.get("sources", []),
        }
        for r in report.get("checked", [])
    ]


def _topics(report: Dict[str, Any], vid: str) -> str:
    topics = report.get("topics")
    legacy = topics is None
    if not topics:  # a report from before v3, or a run where the topic step failed: show the key ideas instead
        topics = [
            {"title": kp["concept"], "timestamp": kp["timestamp"], "summary": kp["point"], "source_urls": []}
            for kp in report["summary"]["key_points"]
        ]
    if not topics:
        return '<p class="empty">This video has no summary.</p>'
    cards = "".join(
        f'<article class="topic"><h3>{esc(t["title"])} <span class="ts">{ts_link(vid, t["timestamp"])}</span></h3>'
        f'<p>{esc(t["summary"])}</p>{_source_links(t.get("source_urls", []), "sources topic-sources")}</article>'
        for t in topics
    )
    hint = '<p class="meta">This report predates topic summaries. Re-run the analysis to get them.</p>' if legacy else ""
    return hint + cards


def _fact_table(report: Dict[str, Any], vid: str) -> str:
    rows = _fact_rows(report)
    if not rows:
        return '<p class="empty">No claims in this video could be fact-checked.</p>'
    body = "".join(
        f'<tr><td class="col-time">{ts_link(vid, r["timestamp"])}</td><td class="col-claim">{esc(r["claim"])}</td>'
        f'<td class="col-verdict"><span class="verdict verdict-{_RATING_CLASS.get(r["rating"], "none")}" '
        f'title="{esc(r["verdict"])}{" (" + esc(r["confidence"]) + " confidence)" if r.get("confidence") else ""}">{esc(r["rating"])}</span></td>'
        f'<td class="col-sources">{_source_links(r.get("sources", [])) or "—"}</td></tr>'
        for r in rows
    )
    return (
        '<div class="table-wrap"><table class="fact-table"><thead><tr><th>Time</th><th>Claim</th><th>Verdict</th><th>Sources</th></tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def render_report_html(report: Dict[str, Any], aborted: bool = False) -> str:
    vid = report["video_id"]
    hl, byw = report["headline"], report["before_you_watch"]
    label, cls = _CLICKBAIT.get(hl.get("clickbait_rating", ""), ("n/a", "muted"))
    chips = [
        f'<span class="chip">{esc(report["video_type"])}</span>',
        f'<span class="chip {cls}">Clickbait: {esc(label)}</span>',
        _percent_chip(percent_correct(report.get("checked", [])), report.get("reliability", {})),
    ]
    if byw["has_paid_promotion"]:
        chips.append(_sponsor_chip(byw["sponsors"]))

    title_check = ""
    if hl.get("verdict"):
        title_check = (
            f'<p class="title-check"><strong>Title check:</strong> {esc(hl["verdict"])} '
            f'<span class="meta">(answer at {ts_link(vid, hl.get("payoff_timestamp", ""))})</span></p>'
        )
    head = f'<div class="r-head"><h2>{esc(report["title"])}</h2><div class="chips">{"".join(chips)}</div></div>'
    rows = _fact_rows(report)
    tabs = (
        '<div class="tabs r-tabs" role="tablist" aria-label="Report views">'
        '<button type="button" role="tab" id="rtab-summary" class="tab active" aria-selected="true" aria-controls="rpanel-summary" data-tab="summary">Summary</button>'
        f'<button type="button" role="tab" id="rtab-facts" class="tab" aria-selected="false" aria-controls="rpanel-facts" data-tab="facts">Fact check <span class="count">{len(rows)}</span></button>'
        "</div>"
    )
    summary_panel = (
        f'<section role="tabpanel" id="rpanel-summary" class="r-panel" aria-labelledby="rtab-summary">'
        f'<p class="tldr">{esc(report["summary"]["tldr"])}</p>{title_check}{_topics(report, vid)}</section>'
    )
    facts_panel = (
        f'<section role="tabpanel" id="rpanel-facts" class="r-panel" aria-labelledby="rtab-facts" hidden>{_fact_table(report, vid)}</section>'
    )
    flag = ' data-aborted="1"' if aborted else ""
    return f'<article class="report v3" data-video-id="{esc(vid)}"{flag}>{head}{tabs}{summary_panel}{facts_panel}</article>'

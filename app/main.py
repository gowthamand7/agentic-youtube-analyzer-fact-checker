"""FastAPI backend: SSE streaming analysis endpoint, JSON endpoint, and HTMX UI fragments."""

from __future__ import annotations

import html
import logging
import re
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from pydantic import BaseModel

from agent.report import timestamp_to_seconds

log = logging.getLogger("app")

STATIC_DIR = Path(__file__).parent / "static"
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")

NODE_LABELS = {
    "triage": "Triage & channel history",
    "extraction": "Reading transcript & comments",
    "investigator": "Investigating claims",
    "critic": "Self-review",
    "report": "Scoring & building report",
}

Runner = Callable[[str], AsyncGenerator[Dict[str, Any], None]]

# html=False escapes raw HTML in LLM-written text; markdown-it also rejects javascript: links.
_md = MarkdownIt("commonmark", {"html": False}).enable("table")

app = FastAPI(title="Agentic YouTube Analyzer")


def is_youtube_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in YOUTUBE_HOSTS:
        return False
    return bool(VIDEO_ID_RE.search(parsed.query + parsed.path))


def valid_video_url(video_url: str = Query(..., max_length=300)) -> str:
    if not is_youtube_url(video_url):
        raise HTTPException(status_code=422, detail="video_url must be a YouTube video URL")
    return video_url.strip()


def get_runner() -> Runner:
    from agent.graph import run_agent

    return run_agent


class AnalyzeRequest(BaseModel):
    video_url: str


def sse(event: str, data: str) -> str:
    lines = data.splitlines() or [""]
    return f"event: {event}\n" + "".join(f"data: {line}\n" for line in lines) + "\n"


def _badge(text: str, kind: str) -> str:
    return f'<li class="badge badge-{kind}">{html.escape(text)}</li>'


def progress_html(event: Dict[str, Any]) -> str | None:
    kind = event.get("type")
    if kind == "node_start":
        label = NODE_LABELS.get(event["node"], event["node"])
        if event["node"] == "investigator" and event.get("attempt"):
            label += f" (retry {event['attempt']})"
        return _badge(label, "node")
    if kind == "tool_start":
        return _badge(f"tool: {event['tool']}", "tool")
    return None


_TABLE_RE = re.compile(r"(?m)^\| Timestamp.*(?:\n\|.*)*")
_CLAIMS_MARK = "[[CLAIMS]]"
_VERDICT_CLASS = {
    "Verified": "good", "Mostly True": "good", "Exaggerated": "warn",
    "Partially False": "warn", "Debunked": "bad", "Unverified": "none",
}


def _ts_link(video_id: str, ts: str) -> str:
    secs = timestamp_to_seconds(ts)
    if secs is None or not VIDEO_ID_RE.fullmatch(video_id):
        return html.escape(ts or "")
    return f'<a href="https://youtube.com/watch?v={video_id}&amp;t={secs}s" target="_blank" rel="noopener">{html.escape(ts)}</a>'


def claims_accordion(state: Dict[str, Any]) -> str:
    """One expandable <details> card per fact-checked claim."""
    video_id = state.get("video_id", "")
    cards = []
    for r in state.get("verification_results", []):
        cls = _VERDICT_CLASS.get(r.get("verdict", ""), "none")
        sources = "".join(
            f'<li><a href="{html.escape(u)}" target="_blank" rel="noopener noreferrer">{html.escape(u)}</a></li>'
            for u in r.get("sources", [])
            if u.startswith(("http://", "https://"))
        )
        cards.append(
            f'<details class="claim claim-{cls}"><summary>'
            f'<span class="verdict verdict-{cls}">{html.escape(r.get("verdict", ""))}</span>'
            f'<span class="claim-text">{html.escape(r.get("claim", ""))}</span></summary>'
            f'<div class="claim-body"><p class="meta">At {_ts_link(video_id, r.get("timestamp", ""))}'
            f' · confidence: {html.escape(str(r.get("confidence", "n/a")))}</p>'
            f'<p>{html.escape(r.get("evidence", ""))}</p>'
            + (f'<ul class="sources">{sources}</ul>' if sources else "")
            + "</div></details>"
        )
    return '<div class="claims">' + "".join(cards) + "</div>" if cards else ""


def report_html(state: Dict[str, Any]) -> str:
    score = state.get("overall_accuracy_score")
    head = ""
    if score is not None and not state.get("error_message"):
        head = f'<p class="score">Overall accuracy <strong>{int(score)}/100</strong></p>'
    md = state.get("final_report_md", "")
    accordion = claims_accordion(state)
    if accordion:
        md = _TABLE_RE.sub(_CLAIMS_MARK, md, count=1)
    body = _md.render(md)
    if accordion:
        body = body.replace(f"<p>{_CLAIMS_MARK}</p>", accordion)
    return head + f'<article class="report">{body}</article>'


async def stream_events(runner: Runner, video_url: str) -> AsyncGenerator[str, None]:
    try:
        async for event in runner(video_url):
            if event.get("type") == "final":
                yield sse("report", report_html(event["state"]))
            elif (badge := progress_html(event)) is not None:
                yield sse("progress", badge)
    except Exception as exc:
        log.exception("analysis failed")
        yield sse("error", f'<p class="error">Analysis failed: {html.escape(str(exc))}</p>')
    yield sse("done", "complete")


@app.get("/api/analyze/stream")
async def analyze_stream(video_url: str = Depends(valid_video_url), runner: Runner = Depends(get_runner)):
    return StreamingResponse(
        stream_events(runner, video_url),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/analyze")
async def analyze(body: AnalyzeRequest, runner: Runner = Depends(get_runner)):
    video_url = valid_video_url(body.video_url)
    final: Dict[str, Any] = {}
    try:
        async for event in runner(video_url):
            if event.get("type") == "final":
                final = event["state"]
    except Exception as exc:
        log.exception("analysis failed")
        raise HTTPException(status_code=502, detail=f"Analysis failed: {exc}") from exc
    return {
        "report_md": final.get("final_report_md", ""),
        "overall_accuracy_score": final.get("overall_accuracy_score"),
        "saved_to_memory": final.get("saved_to_memory", False),
        "error_message": final.get("error_message"),
    }


@app.get("/ui/panel", response_class=HTMLResponse)
async def ui_panel(video_url: str = Depends(valid_video_url)):
    src = f"/api/analyze/stream?video_url={quote(video_url, safe='')}"
    return (
        f'<div hx-ext="sse" sse-connect="{html.escape(src)}" sse-close="done" class="panel">'
        '<ul class="steps" sse-swap="progress" hx-swap="beforeend"></ul>'
        '<div sse-swap="error"></div>'
        '<div sse-swap="report"></div>'
        "</div>"
    )


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

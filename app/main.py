"""FastAPI backend: SSE streaming analysis endpoint, JSON endpoint, and the static UI."""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from pydantic import BaseModel

from agent.knowledge import KnowledgeStore
from agent.personalize import find_item, personalize_report
from agent.report import timestamp_to_seconds
from agent.report_store import load_report, video_id_from_url
from app.render import render_report_html

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("app")

STATIC_DIR = Path(__file__).parent / "static"
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")

PROGRESS_EVENT_TYPES = {
    "request", "node_start", "node_end", "tool_start", "tool_end", "note", "video", "overview", "claims", "verdict",
    "trace", "summary", "comments",
}

Runner = Callable[[str], AsyncGenerator[Dict[str, Any], None]]
Personalizer = Callable[[Dict[str, Any]], Awaitable[Dict[str, Dict[str, Any]]]]

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


def get_knowledge_store() -> KnowledgeStore:
    return KnowledgeStore()


def get_personalizer(store: KnowledgeStore = Depends(get_knowledge_store)) -> Personalizer:
    """Annotates a report with the items this user already knows (nothing happens until they mark something)."""

    async def personalize(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        if not store.entries():
            return {}
        from agent.graph import default_llm

        return await personalize_report(report, store, default_llm())

    return personalize


class AnalyzeRequest(BaseModel):
    video_url: str
    refresh: bool = False  # True ignores the stored report and runs a new analysis


def sse(event: str, data: str) -> str:
    lines = data.splitlines() or [""]
    return f"event: {event}\n" + "".join(f"data: {line}\n" for line in lines) + "\n"


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


def report_html(state: Dict[str, Any], notes: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """v2 reports render from their structure (with personalization); older stored reports fall back to Markdown."""
    if state.get("report") and not state.get("error_message"):
        return render_report_html(state["report"], notes)
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
    aborted = ' data-aborted="1"' if state.get("error_message") else ""
    return head + f'<article class="report"{aborted}>{body}</article>'


async def _no_personalization(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {}


def stored_report(video_url: str, refresh: bool) -> Optional[Dict[str, Any]]:
    """The previously generated report for this video, unless the caller asked for a fresh run."""
    video_id = None if refresh else video_id_from_url(video_url)
    return load_report(video_id) if video_id else None


async def personalized_report_html(state: Dict[str, Any], personalizer: Personalizer) -> str:
    notes = await personalizer(state["report"]) if state.get("report") and not state.get("error_message") else None
    return report_html(state, notes)


async def stream_events(
    runner: Runner, video_url: str, refresh: bool = False, personalizer: Optional[Personalizer] = None
) -> AsyncGenerator[str, None]:
    """SSE frames: `agent` (JSON progress event), `cached` + `report` (stored report), `report`, `error`, then `done`.

    A stored report is served without running the agent; `refresh=True` forces a new analysis.
    """
    personalizer = personalizer or _no_personalization
    stored = stored_report(video_url, refresh)
    if stored:
        meta = {"generated_at": stored["generated_at"], "video": stored.get("video", {}), "video_id": stored["state"].get("video_id")}
        yield sse("cached", json.dumps(meta, ensure_ascii=False))
        yield sse("report", await personalized_report_html(stored["state"], personalizer))
        yield sse("done", "complete")
        return
    try:
        async for event in runner(video_url):
            if event.get("type") == "final":
                yield sse("report", await personalized_report_html(event["state"], personalizer))
            elif event.get("type") in PROGRESS_EVENT_TYPES:
                yield sse("agent", json.dumps(event, ensure_ascii=False, default=str))
    except Exception as exc:
        log.exception("analysis failed")
        yield sse("error", f"Analysis failed: {exc}")
    yield sse("done", "complete")


@app.get("/api/analyze/stream")
async def analyze_stream(
    video_url: str = Depends(valid_video_url),
    refresh: bool = False,
    runner: Runner = Depends(get_runner),
    personalizer: Personalizer = Depends(get_personalizer),
):
    return StreamingResponse(
        stream_events(runner, video_url, refresh, personalizer),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/analyze")
async def analyze(
    body: AnalyzeRequest, runner: Runner = Depends(get_runner), personalizer: Personalizer = Depends(get_personalizer)
):
    video_url = valid_video_url(body.video_url)
    stored = stored_report(video_url, body.refresh)
    if stored:
        state = stored["state"]
        return {
            "report": state.get("report"),
            "known": await personalizer(state["report"]) if state.get("report") else {},
            "report_md": state.get("final_report_md", ""),
            "overall_accuracy_score": state.get("overall_accuracy_score"),
            "saved_to_memory": state.get("saved_to_memory", False),
            "error_message": None,
            "cached": True,
            "generated_at": stored["generated_at"],
        }
    final: Dict[str, Any] = {}
    try:
        async for event in runner(video_url):
            if event.get("type") == "final":
                final = event["state"]
    except Exception as exc:
        log.exception("analysis failed")
        raise HTTPException(status_code=502, detail=f"Analysis failed: {exc}") from exc
    return {
        "report": final.get("report"),
        "known": await personalizer(final["report"]) if final.get("report") else {},
        "report_md": final.get("final_report_md", ""),
        "overall_accuracy_score": final.get("overall_accuracy_score"),
        "saved_to_memory": final.get("saved_to_memory", False),
        "error_message": final.get("error_message"),
        "cached": False,
        "generated_at": None,
    }


class KnowOrder(BaseModel):
    video_id: str
    item_id: str


@app.get("/api/knowledge")
async def list_knowledge(store: KnowledgeStore = Depends(get_knowledge_store)):
    return {"entries": store.entries()}


@app.post("/api/knowledge")
async def mark_known(order: KnowOrder, store: KnowledgeStore = Depends(get_knowledge_store)):
    """Record "I already know this". Text comes from the stored report, never from the client, so it cannot be forged."""
    stored = load_report(order.video_id)
    report = (stored or {}).get("state", {}).get("report")
    item = find_item(report, order.item_id) if report else None
    if not item:
        raise HTTPException(status_code=404, detail="No such item in the saved report for that video")
    return store.add(item["concept"], item["text"], report.get("topic_tags", []), order.video_id, report.get("title", ""))


@app.delete("/api/knowledge/{entry_id}")
async def forget_known(entry_id: str, store: KnowledgeStore = Depends(get_knowledge_store)):
    if not re.fullmatch(r"[a-f0-9]{10}", entry_id) or not store.remove(entry_id):
        raise HTTPException(status_code=404, detail="No such knowledge entry")
    return {"removed": entry_id}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

"""Generate docs/architecture.svg, the one-page architecture diagram of the fact-checking agent.

Run: python scripts/make_architecture_diagram.py   (then open docs/architecture.svg, or render it to PNG).
Edit the coordinates below to change the layout; every box is described once, in data.
"""

from __future__ import annotations

import html
import textwrap
from pathlib import Path

W, H = 1250, 1500
FONT = "'Segoe UI', 'Helvetica Neue', Arial, sans-serif"

# name: (stroke, fill)
C = {
    "slate": ("#475569", "#f1f5f9"),
    "teal": ("#0f766e", "#d9f5ef"),
    "purple": ("#6d28d9", "#ede9fe"),
    "amber": ("#b45309", "#fef3c7"),
    "indigo": ("#4338ca", "#e0e7ff"),
    "sky": ("#0369a1", "#e0f2fe"),
    "green": ("#15803d", "#dcfce7"),
    "orange": ("#c2410c", "#ffedd5"),
    "red": ("#b91c1c", "#fee2e2"),
    "blue": ("#1d4ed8", "#dbeafe"),
    "cyan": ("#0e7490", "#cffafe"),
}
INK, MUTED = "#1e293b", "#475569"
out: list[str] = []


def e(s: str) -> str:
    return html.escape(s, quote=True)


def add(s: str) -> None:
    out.append(s)


def text(x, y, s, size=12, weight=400, fill=INK, anchor="start", extra="") -> None:
    add(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}" {extra}>{e(s)}</text>')


def para(x, y, s, size=11.5, width=380, fill=MUTED, lh=None, anchor="start", weight=400) -> float:
    """Word-wrapped text; returns the y of the line after the last one."""
    lh = lh or size * 1.35
    chars = max(8, int(width / (size * 0.50)))
    for line in textwrap.wrap(s, chars):
        text(x, y, line, size, weight, fill, anchor)
        y += lh
    return y


def box(x, y, w, h, color, rx=12, sw=2, dash=None, fill=None, opacity=1) -> None:
    stroke, f = C[color]
    d = f' stroke-dasharray="{dash}"' if dash else ""
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill or f}" stroke="{stroke}" stroke-width="{sw}"{d} opacity="{opacity}"/>')


def badge(cx, cy, label, color, r=13) -> None:
    add(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{C[color][0]}"/>')
    text(cx, cy + 4.5, label, 13, 700, "#fff", "middle")


def tag(x, y, s, color) -> None:
    """Small stage label, right-aligned at x."""
    w = len(s) * 6.4 + 16
    add(f'<rect x="{x - w}" y="{y}" width="{w}" height="18" rx="9" fill="{C[color][0]}"/>')
    text(x - w / 2, y + 12.5, s, 10, 700, "#fff", "middle", 'letter-spacing=".6"')


def arrow(d, color="#475569", dash=None, sw=2.2, start=False) -> None:
    mid = "ah-" + color.strip("#")
    s = f' marker-start="url(#{"ah-" + color.strip("#")})"' if start else ""
    da = f' stroke-dasharray="{dash}"' if dash else ""
    add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}"{da} marker-end="url(#{mid})"{s}/>')


def node(x, y, w, h, color, num, title, lines, stage):
    box(x, y, w, h, color)
    badge(x + 24, y + 26, num, color)
    text(x + 48, y + 31, title, 15, 700)
    tag(x + w - 10, y + 10, stage, color)
    ty = y + 52
    for ln in lines:
        ty = para(x + 16, ty, ln, 11.5, w - 32)


def diamond(cx, cy, hw, hh, color, l1, l2=""):
    stroke, fill = C[color]
    add(f'<polygon points="{cx},{cy - hh} {cx + hw},{cy} {cx},{cy + hh} {cx - hw},{cy}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
    text(cx, cy - (2 if l2 else -4), l1, 13, 700, INK, "middle")
    if l2:
        text(cx, cy + 14, l2, 10.5, 400, MUTED, "middle")


# ------------------------------------------------------------------ canvas
add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{FONT}">')
add("<title>Architecture of the agentic YouTube analyzer and fact-checker</title>")
markers = {c.strip("#") for c in ("#475569", "#b91c1c", "#6d28d9", "#c2410c", "#15803d", "#1d4ed8")}
add("<defs>")
for m in sorted(markers):
    add(f'<marker id="ah-{m}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#{m}"/></marker>')
add("</defs>")
add(f'<rect width="{W}" height="{H}" fill="#fbfaf7"/>')
add(f'<rect x="8" y="8" width="{W - 16}" height="{H - 16}" rx="10" fill="none" stroke="#94a3b8" stroke-width="1.5" stroke-dasharray="6 5"/>')

text(W / 2, 58, "How the YouTube Fact-Checking Agent Works", 32, 700, INK, "middle")
text(W / 2, 88, "From a video link to a topic-by-topic summary and a source-linked fact check", 15, 400, MUTED, "middle")

# ------------------------------------------------------------------ entry
CX = 475
box(325, 118, 300, 56, "slate")
add('<circle cx="355" cy="139" r="6" fill="none" stroke="#475569" stroke-width="2"/><path d="M344 158 q11 -16 22 0" fill="none" stroke="#475569" stroke-width="2"/>')
text(380, 142, "Input: YouTube video link", 14, 700)
text(380, 160, "Web UI  ·  REST API  ·  live SSE stream", 11, 400, MUTED)
arrow(f"M{CX},174 L{CX},202")

box(325, 202, 300, 56, "slate")
text(CX, 226, "FastAPI backend", 14, 700, INK, "middle")
text(CX, 244, "app/main.py  ·  streams progress as events", 11, 400, MUTED, "middle")
arrow(f"M{CX},258 L{CX},286")

diamond(CX, 328, 100, 42, "amber", "Report saved?", "")
text(CX + 108, 322, "YES", 11, 700, "#b45309")
text(CX + 8, 392, "NO, or Re-run", 11, 700, "#b45309")
box(620, 303, 140, 50, "slate")
text(690, 325, "Show saved report", 12, 700, INK, "middle")
text(690, 341, "instantly, no model calls", 10.5, 400, MUTED, "middle")
arrow(f"M{CX + 100},328 L620,328")
# saved report goes straight to the output panel, around the right edge
arrow("M760,328 L1228,328 L1228,1000 L1203,1000", "#475569", dash="6 5")
text(1000, 320, "saved report is served straight to the UI", 10.5, 400, MUTED, "middle")
arrow(f"M{CX},370 L{CX},402")

# ------------------------------------------------------------------ agent container
box(190, 402, 570, 684, "purple", rx=16, dash="8 6", fill="#faf8ff", sw=1.8)
text(206, 424, "LangGraph agent", 13, 700, "#6d28d9")
text(318, 424, "claude-haiku-5-5  ·  5 steps, with a retry loop", 12, 400, MUTED)
NX, NW = 250, 450

node(NX, 440, NW, 72, "teal", "1", "Triage", [
    "Video details from the YouTube Data API, plus the channel's track record from past runs."], "PERCEIVE")
arrow(f"M{CX},512 L{CX},536")
node(NX, 536, NW, 108, "teal", "2", "Extraction", [
    "Transcript: scraper first, Supadata fallback, cached. Top 100 comments with their replies, from the official API.",
    "Two model calls in parallel: video summary + claims to check, and useful points from comments."], "PERCEIVE")
arrow(f"M{CX},644 L{CX},668")
node(NX, 668, NW, 100, "purple", "3", "Investigator", [
    "A model-driven tool loop that states its reasoning before each call.",
    "A verdict needs a source link, otherwise it stays Unverified."], "THINK + ACT")
arrow(f"M{CX},768 L{CX},790")
diamond(CX, 830, 100, 40, "amber", "Weak or unverified", "high / medium claims?")
arrow(f"M{CX - 100},830 L222,830 L222,718 L{NX - 1},718", "#6d28d9")
add(f'<text x="210" y="780" font-size="11" font-weight="700" fill="#6d28d9" text-anchor="middle" transform="rotate(-90 210 780)">YES: sharper queries, max 2 retries</text>')
text(CX + 8, 886, "NO", 11, 700, "#b45309")
arrow(f"M{CX},870 L{CX},902")
node(NX, 902, NW, 80, "indigo", "4", "Synthesis", [
    "One short paragraph per topic, in plain first-party voice, from the transcript, the evidence and the comments."], "WRITE")
arrow(f"M{CX},982 L{CX},1006")
node(NX, 1006, NW, 70, "sky", "5", "Commit and report", [
    "Builds the report, saves it to ClickHouse and stores it for reuse."], "SAVE")

# tools underneath
arrow(f"M{CX},1086 L{CX},1138", "#15803d")
text(CX + 10, 1118, "tool calls over MCP (stdio)", 11, 700, "#15803d")

# ------------------------------------------------------------------ tool layer
box(190, 1138, 570, 168, "green", rx=14, dash="8 6", fill="#f4fdf6", sw=1.6)
text(206, 1160, "Tool layer: four MCP servers", 13, 700, "#15803d")
cards = [
    ("youtube-mcp", "Python", "YouTube Data API, transcript scraper, Supadata fallback, disk cache"),
    ("verify-social-mcp", "Go", "Tavily web search, Hacker News, Bluesky, Stack Exchange"),
    ("sandbox-mcp", "Go", "Runs Python audits for numeric claims in a locked-down Docker container"),
    ("memory-db-mcp", "Go", "ClickHouse: channel history, earlier verdicts, saving analyses"),
]
for i, (name, lang, desc) in enumerate(cards):
    cx0 = 200 + i * 138
    box(cx0, 1172, 130, 122, "green", rx=10, sw=1.5, fill="#ffffff")
    text(cx0 + 10, 1192, name, 11.5, 700)
    text(cx0 + 10, 1206, lang, 10, 600, "#15803d")
    para(cx0 + 10, 1222, desc, 10.5, 112, MUTED)

# ------------------------------------------------------------------ memory (right)
box(800, 402, 400, 340, "orange", rx=14)
text(822, 432, "Memory and caches", 16, 700, "#9a3412")
text(822, 460, "Short-term (one request)", 12.5, 700, "#c2410c")
y = para(822, 478, "Agent state: transcript, claims, verdicts, comment insights, topics.", 11.5, 360)
para(822, y + 1, "Per-request JSONL log of every step.", 11.5, 360)
text(822, 548, "Long-term (kept between runs)", 12.5, 700, "#c2410c")
y = para(822, 566, "ClickHouse: channel track record and every verified claim.", 11.5, 360)
y = para(822, y + 1, "Saved reports, reused until you press Re-run.", 11.5, 360)
y = para(822, y + 1, "Caches: transcripts 30 days, video details 6 hours, so YouTube is hit once per video.", 11.5, 360)
para(822, y + 1, "Transcripts: scraper first; Supadata (several keys in rotation) when YouTube blocks it.", 11.5, 360)
arrow(f"M760,600 L798,600", "#c2410c", start=True)

# ------------------------------------------------------------------ output (right)
box(800, 800, 400, 286, "cyan", rx=14)
text(822, 830, "Output: web UI report", 16, 700, "#0e7490")
text(822, 858, "Header strip", 12.5, 700, "#0e7490")
y = para(822, 876, "Category, clickbait level (No / Medium / High), percent of facts correct, paid-promotion flag.", 11.5, 360)
text(822, y + 12, "Summary tab", 12.5, 700, "#0e7490")
y = para(822, y + 30, "Topic-by-topic paragraphs in plain voice, with the sources each one used.", 11.5, 360)
text(822, y + 12, "Fact check tab", 12.5, 700, "#0e7490")
y = para(822, y + 30, "Every checked claim: time, Correct / Doubtful / False, source links.", 11.5, 360)
text(822, y + 12, "Agent activity", 12.5, 700, "#0e7490")
para(822, y + 30, "Live steps with the model's reasoning, and a developer trace of every call.", 11.5, 360)
arrow("M700,1041 L798,1041", "#0e7490")

# ------------------------------------------------------------------ guardrails (left)
box(24, 402, 150, 904, "red", rx=14)
text(99, 436, "Guardrails", 16, 700, "#991b1b", "middle")
text(99, 456, "and safety", 16, 700, "#991b1b", "middle")
items = [
    ("No source, no verdict", "A verdict with no source link is forced back to Unverified."),
    ("No invented links", "A topic can only cite sources that were actually gathered; others are dropped."),
    ("Official APIs only", "Comments come from the YouTube Data API; no scraping of comments."),
    ("Sandboxed code", "Python audits run in Docker: no network, read-only disk, memory limit."),
    ("Safe rendering", "All model text is HTML-escaped; links are limited to http(s)."),
    ("Clean failures", "Aborted runs are never saved. A bad model answer is retried once with the error shown to it."),
    ("Bounded cost", "Claims, tool rounds, retries and comment text are all capped."),
]
y = 488
for head, body in items:
    text(38, y, head, 11.5, 700, "#991b1b")
    y = para(38, y + 15, body, 10.5, 124, "#7f1d1d") + 10
arrow("M174,480 L188,480", "#b91c1c")

# ------------------------------------------------------------------ observability bar
box(190, 1330, 1010, 118, "blue", rx=14)
badge(216, 1358, "6", "blue")
text(240, 1363, "Observe: you can see exactly what the agent did", 15, 700, "#1e3a8a")
cols = [
    ("Steps tab", "Plain-language timeline with the model's reasoning before each tool call."),
    ("Developer trace", "Every tool call with its arguments and result, every model call, with timings. Copyable."),
    ("Request logs", "One JSONL file per run in logs/, read with scripts/show_log.py."),
    ("Tests", "Automated tests for the agent, report and API, plus live runs on real videos."),
]
for i, (head, body) in enumerate(cols):
    x0 = 210 + i * 250
    text(x0, 1392, head, 12, 700, "#1d4ed8")
    para(x0, 1408, body, 10.5, 230, "#1e3a8a")

text(W / 2, 1478, "Built with LangGraph  ·  MCP  ·  FastAPI  ·  ClickHouse  ·  Docker  ·  Claude Haiku 5.5", 12, 400, MUTED, "middle")
add("</svg>")

target = Path(__file__).resolve().parent.parent / "docs" / "architecture.svg"
target.parent.mkdir(exist_ok=True)
target.write_text("\n".join(out), encoding="utf-8")
print(f"wrote {target} ({target.stat().st_size} bytes)")

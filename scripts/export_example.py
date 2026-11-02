"""Export a saved report as a standalone PDF and/or PNG, for examples in the docs.

Run: python scripts/export_example.py <videoId> <name> [--pdf] [--png]
Reads reports/<videoId>.json, renders it with the same code as the web UI (Summary and Fact check stacked, light theme)
and prints it with a headless Edge or Chrome. Writes docs/examples/<name>.pdf and, for --png, docs/<name>.png.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.report_store import load_report  # noqa: E402
from app.render import render_report_html  # noqa: E402

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
PAGE_WIDTH = 960

EXTRA_CSS = """
:root, :root[data-theme] { color-scheme: light; }
* { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { background: #f8fafc; }
main { padding: 1.5rem 1.25rem 2rem; }
.ex-note { color: var(--muted); font-size: .8rem; margin: 0 0 1rem; }
.ex-h { font-size: 1.15rem; margin: 1.8rem 0 .7rem; padding-bottom: .3rem; border-bottom: 1px solid var(--border); }
.topic, .fact-table tr { break-inside: avoid; }
@page { size: A4; margin: 12mm; }
"""


def find_browser() -> str:
    env = os.environ.get("BROWSER_BIN")
    for candidate in ([env] if env else []) + BROWSERS:
        if candidate and Path(candidate).exists():
            return candidate
    sys.exit("No Edge or Chrome found. Set BROWSER_BIN to a Chromium-based browser.")


def build_page(video_id: str) -> str:
    record = load_report(video_id)
    report = (record or {}).get("state", {}).get("report")
    if not report:
        sys.exit(f"No v3 report saved for {video_id} (looked in reports/).")
    body = render_report_html(report)
    body = re.sub(r'<div class="tabs r-tabs".*?</div>', "", body, count=1, flags=re.S)  # both panels are shown, so no tab buttons
    body = body.replace('aria-labelledby="rtab-facts" hidden>', 'aria-labelledby="rtab-facts">')
    body = body.replace('<section role="tabpanel" id="rpanel-summary"', '<h3 class="ex-h">Summary</h3><section role="tabpanel" id="rpanel-summary"')
    body = body.replace('<section role="tabpanel" id="rpanel-facts"', '<h3 class="ex-h">Fact check</h3><section role="tabpanel" id="rpanel-facts"')
    css = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")
    note = (
        f'<p class="ex-note">Example output for https://www.youtube.com/watch?v={html.escape(video_id)}, generated {html.escape(record["generated_at"][:10])}. '
        "Written by a language model from the transcript, web search and comments; it can contain mistakes.</p>"
    )
    return f'<!doctype html><html lang="en" data-theme="light"><head><meta charset="utf-8"><style>{css}{EXTRA_CSS}</style></head><body><main>{note}{body}</main></body></html>'


def run(browser: str, args: list[str]) -> None:
    subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", *args], check=True, capture_output=True, timeout=120)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id")
    ap.add_argument("name")
    ap.add_argument("--pdf", action="store_true")
    ap.add_argument("--png", action="store_true")
    args = ap.parse_args()
    if not (args.pdf or args.png):
        args.pdf = True
    browser = find_browser()
    tmp = Path(tempfile.mkdtemp(prefix="example_"))
    page = tmp / "report.html"
    page.write_text(build_page(args.video_id), encoding="utf-8")
    url = page.as_uri()
    try:
        if args.pdf:
            target = ROOT / "docs" / "examples" / f"{args.name}.pdf"
            target.parent.mkdir(parents=True, exist_ok=True)
            run(browser, ["--no-pdf-header-footer", f"--print-to-pdf={target}", url])
            print(f"wrote {target}")
        if args.png:
            target = ROOT / "docs" / f"{args.name}.png"
            # Measure the page, then take a screenshot exactly that tall.
            probe = tmp / "probe.html"
            probe.write_text(
                page.read_text(encoding="utf-8").replace("</body>", "<script>document.title=document.documentElement.scrollHeight</script></body>"),
                encoding="utf-8",
            )
            dom = subprocess.run(
                [browser, "--headless=new", "--disable-gpu", f"--window-size={PAGE_WIDTH},800", "--virtual-time-budget=2000", "--dump-dom", probe.as_uri()],
                capture_output=True, text=True, encoding="utf-8", check=True, timeout=120,
            ).stdout
            m = re.search(r"<title>(\d+)</title>", dom)
            height = int(m.group(1)) + 24 if m else 3000
            run(browser, [f"--window-size={PAGE_WIDTH},{height}", f"--screenshot={target}", url])
            print(f"wrote {target} ({PAGE_WIDTH}x{height})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

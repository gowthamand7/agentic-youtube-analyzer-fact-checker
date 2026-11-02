#!/usr/bin/env python3
"""Pretty-print a per-request debug log as a timeline.

Usage: python scripts/show_log.py [latest | <request id> | <path>] [-v]
  -v  also print tool arguments/results and LLM outputs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "logs"


def find_log(target: str, directory: Path) -> Path:
    if Path(target).is_file():
        return Path(target)
    files = sorted(directory.glob("*.jsonl"))
    if not files:
        raise SystemExit(f"No logs in {directory}")
    if target == "latest":
        return files[-1]
    matches = [f for f in files if target in f.name]
    if not matches:
        raise SystemExit(f"No log matching {target!r} in {directory}")
    return matches[-1]


def describe(rec: dict) -> str:
    ev = rec["event"]
    if ev == "request_start":
        return f"START  {rec.get('video_url')}"
    if ev == "request_end":
        extra = f" error={rec['error']}" if rec.get("error") else ""
        return f"END    status={rec.get('status')} score={rec.get('score')} saved={rec.get('saved_to_memory')} total={rec.get('duration_ms')}ms{extra}"
    if ev == "request_error":
        return "ERROR  unhandled exception (traceback below)"
    if ev == "request_cancelled":
        return f"CANCEL {rec.get('reason')}"
    if ev == "tool_call":
        flag = "ok  " if rec.get("ok") else "FAIL"
        return f"TOOL   {flag} {rec['tool']} ({rec.get('duration_ms')}ms) -> {rec.get('summary')}"
    if ev == "llm_call":
        flag = "ok  " if rec.get("ok") else "FAIL"
        return f"LLM    {flag} {rec.get('purpose')} ({rec.get('duration_ms')}ms) -> {rec.get('summary', '')}"
    if ev == "llm_turn":
        calls = ", ".join(c["name"] for c in rec.get("tool_calls", [])) or "no tool calls"
        return f"TURN   round {rec.get('round')}: {calls}"
    if ev == "verdict":
        return f"VERDICT {rec['verdict']} ({rec['confidence']}): {rec['claim']}"
    if ev == "node_end":
        return f"NODE   finished {rec['node']}"
    if ev == "progress":
        kind = rec.get("type")
        if kind == "node_start":
            return f"NODE   starting {rec.get('node')}"
        if kind == "note":
            return f"NOTE   {rec.get('text')}"
        return f"EVENT  {kind}"
    return ev


def verbose_lines(rec: dict) -> list[str]:
    lines = []
    for key in ("args", "result", "output", "prompt", "text"):
        if rec.get(key):
            lines.append(f"         {key}: {rec[key]}")
    if rec.get("error"):
        lines.append("         " + str(rec["error"]).rstrip().replace("\n", "\n         "))
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", nargs="?", default="latest")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    path = find_log(args.target, Path(os.environ.get("LOG_DIR") or DEFAULT_DIR))
    print(f"# {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        print(f"{rec['t_ms'] / 1000:7.2f}s  {describe(rec)}")
        if args.verbose or rec["event"] in ("request_error",) or (rec["event"] in ("tool_call", "llm_call") and not rec.get("ok")):
            for extra in verbose_lines(rec):
                print(extra)
    return 0


if __name__ == "__main__":
    sys.exit(main())

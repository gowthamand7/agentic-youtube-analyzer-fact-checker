#!/usr/bin/env python3
"""End-to-end CLI: analyze one YouTube URL against the live MCP servers and print the report."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.graph import run_agent  # noqa: E402


async def main(url: str) -> int:
    final = {}
    async for event in run_agent(url):
        kind = event["type"]
        if kind == "node_start":
            print(f"[node] {event['node']}", file=sys.stderr)
        elif kind == "tool_start":
            print(f"  [tool] {event['tool']}", file=sys.stderr)
        elif kind == "final":
            final = event["state"]
    print(final.get("final_report_md", "(no report produced)"))
    if final.get("error_message"):
        print(f"\nAborted: {final['error_message']}", file=sys.stderr)
        return 2
    print(f"\nscore={final.get('overall_accuracy_score')} saved_to_memory={final.get('saved_to_memory')}", file=sys.stderr)
    return 0 if final.get("saved_to_memory") else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    sys.exit(asyncio.run(main(parser.parse_args().url)))

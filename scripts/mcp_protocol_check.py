#!/usr/bin/env python3
"""Launch all four MCP servers over stdio and assert each exposes its expected tools."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.mcp_client import open_tools  # noqa: E402

EXPECTED_TOOLS = {
    "memory-db-mcp": {"get_channel_history", "save_video_analysis", "query_historical_claims"},
    "verify-social-mcp": {"search_web_factcheck", "search_social_reactions"},
    "sandbox-mcp": {"execute_python_audit"},
    "youtube-mcp": {"get_video_metadata", "get_transcript", "get_comments"},
}


async def main() -> int:
    async with open_tools() as tools:
        available = set(tools)
        failed = False
        for server, expected in EXPECTED_TOOLS.items():
            missing = expected - available
            failed |= bool(missing)
            print(f"{'FAIL' if missing else 'OK  '} {server}: {', '.join(sorted(expected - missing))}" + (f" (missing: {', '.join(sorted(missing))})" if missing else ""))

        probe = await tools["execute_python_audit"].ainvoke({"code": "print(123 * 456)", "timeout_sec": 10})
        sandbox_ok = "56088" in str(probe)
        print(f"{'OK  ' if sandbox_ok else 'FAIL'} sandbox round-trip: print(123 * 456) -> {'56088' if sandbox_ok else probe}")
        return 1 if failed or not sandbox_ok else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

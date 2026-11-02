"""Launches the four MCP servers over stdio and exposes their tools by name."""

from __future__ import annotations

import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Dict

from dotenv import dotenv_values
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

ROOT = Path(__file__).resolve().parent.parent


def server_connections(root: Path = ROOT) -> Dict[str, dict]:
    env = {**os.environ, **{k: v for k, v in dotenv_values(root / ".env").items() if v is not None}}
    exe = ".exe" if sys.platform == "win32" else ""

    def go_server(name: str) -> dict:
        return {"transport": "stdio", "command": str(root / "bin" / f"{name}{exe}"), "args": [], "env": env}

    return {
        "memory-db": go_server("memory-db-mcp"),
        "verify-social": go_server("verify-social-mcp"),
        "sandbox": go_server("sandbox-mcp"),
        "youtube": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(root / "mcp-servers" / "youtube" / "server.py")],
            "env": env,
        },
    }


@asynccontextmanager
async def open_tools(root: Path = ROOT) -> AsyncIterator[Dict[str, BaseTool]]:
    """Start every MCP server once (persistent sessions) and yield {tool_name: tool}."""
    connections = server_connections(root)
    client = MultiServerMCPClient(connections)
    async with AsyncExitStack() as stack:
        tools: Dict[str, BaseTool] = {}
        for name in connections:
            session = await stack.enter_async_context(client.session(name))
            for tool in await load_mcp_tools(session):
                tools[tool.name] = tool
        yield tools

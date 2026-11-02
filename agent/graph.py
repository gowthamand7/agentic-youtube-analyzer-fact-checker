"""Graph wiring and the streaming `run_agent` entry point."""

from __future__ import annotations

import os
from typing import Any, AsyncGenerator, Dict, Mapping

from langgraph.graph import END, START, StateGraph

from agent.mcp_client import open_tools
from agent.nodes import AgentNodes
from agent.state import AgentState


def _after_ingest(next_node: str):
    def route(state: AgentState) -> str:
        return "commit_and_report" if state.get("error_message") else next_node

    return route


def _after_critic(state: AgentState) -> str:
    return "investigator" if state.get("critic_feedback") else "commit_and_report"


def build_graph(tools: Mapping[str, Any], llm: Any):
    n = AgentNodes(tools, llm)
    g = StateGraph(AgentState)
    g.add_node("triage", n.triage_node)
    g.add_node("extraction", n.extraction_node)
    g.add_node("investigator", n.investigator_node)
    g.add_node("critic", n.critic_node)
    g.add_node("commit_and_report", n.commit_and_report_node)

    g.add_edge(START, "triage")
    g.add_conditional_edges("triage", _after_ingest("extraction"), ["extraction", "commit_and_report"])
    g.add_conditional_edges("extraction", _after_ingest("investigator"), ["investigator", "commit_and_report"])
    g.add_edge("investigator", "critic")
    g.add_conditional_edges("critic", _after_critic, ["investigator", "commit_and_report"])
    g.add_edge("commit_and_report", END)
    return g.compile()


def default_llm() -> Any:
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5"), max_tokens=8192)


async def stream_graph(graph: Any, video_url: str) -> AsyncGenerator[Dict[str, Any], None]:
    """Yield progress events ({'type': ...}) and finally {'type': 'final', 'state': {...}}."""
    final: Dict[str, Any] = {}
    async for mode, chunk in graph.astream({"video_url": video_url}, stream_mode=["custom", "updates"]):
        if mode == "custom":
            yield chunk
        else:
            for node_name, update in chunk.items():
                final.update(update or {})
                yield {"type": "node_end", "node": node_name}
    yield {"type": "final", "state": final}


async def run_agent(video_url: str) -> AsyncGenerator[Dict[str, Any], None]:
    """Run the full analysis against the live MCP servers, streaming events then the final report."""
    from dotenv import load_dotenv

    from agent.mcp_client import ROOT

    load_dotenv(ROOT / ".env")
    async with open_tools() as tools:
        async for event in stream_graph(build_graph(tools, default_llm()), video_url):
            yield event

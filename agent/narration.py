"""Plain-language descriptions of tool calls, shown live in the UI activity timeline."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

PLATFORM_NAMES = {"hackernews": "Hacker News", "bluesky": "Bluesky", "reddit": "Reddit", "stackexchange": "Stack Overflow"}


def _short(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_start(tool: str, args: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Return (title, optional detail) for a tool call that is about to run."""
    if tool == "get_video_metadata":
        return "Reading the video's details", "title, channel, description, links, chapters"
    if tool == "get_channel_history":
        return "Checking this channel's track record", None
    if tool == "get_transcript":
        return "Fetching the transcript", None
    if tool == "get_comments":
        return "Reading the top viewer comments", None
    if tool == "search_web_factcheck":
        depth = " (deep search)" if args.get("search_depth") == "advanced" else ""
        return f"Searching the web{depth}", _short(args.get("query", ""))
    if tool == "search_social_reactions":
        names = [PLATFORM_NAMES.get(p, p) for p in args.get("platforms") or []]
        where = ", ".join(names) if names else "community sites"
        return f"Checking what people say on {where}", _short(args.get("topic_query", ""))
    if tool == "execute_python_audit":
        first = (args.get("code") or "").strip().splitlines()[:1]
        return "Running a calculation in the isolated sandbox", _short(first[0]) if first else None
    if tool == "query_historical_claims":
        return "Looking for claims we've already verified", f"topic: {args.get('topic_tag', '')}"
    if tool == "save_video_analysis":
        return "Saving this analysis to the channel's history", None
    return f"Running {tool}", None


def describe_end(tool: str, result: Any) -> Tuple[bool, str]:
    """Return (ok, one-line summary) for a finished tool call."""
    if isinstance(result, dict) and result.get("status") == "ERROR":
        return False, _short(result.get("message", "failed"), 140)
    if isinstance(result, dict) and set(result) == {"raw"}:  # MCP tools report failures as plain, non-JSON text
        return False, _short(result["raw"], 140)
    r = result if isinstance(result, dict) else {}
    if tool == "get_video_metadata":
        return True, f"“{_short(r.get('title', ''), 70)}” by {r.get('channel_name', 'unknown')}"
    if tool == "get_channel_history":
        n = r.get("total_videos_analyzed", 0) or 0
        if not n:
            return True, "First time we've seen this channel"
        return True, f"{n} earlier video{'s' if n != 1 else ''} analyzed, {r.get('avg_accuracy_score', 0)}% average accuracy"
    if tool == "get_transcript":
        if r.get("status") != "OK":
            return False, r.get("message", "No transcript available")
        return True, f"{len(r.get('segments', []))} lines ({r.get('language', '?')})"
    if tool == "get_comments":
        if r.get("status") == "COMMENTS_DISABLED":
            return True, "Comments are turned off"
        return True, f"{len(r.get('comments', []))} comments loaded"
    if tool == "search_web_factcheck":
        results = r.get("results", [])
        domains = []
        for item in results:
            host = item.get("url", "").split("/")[2:3]
            if host and host[0].removeprefix("www.") not in domains:
                domains.append(host[0].removeprefix("www."))
        return True, f"{len(results)} sources" + (f": {', '.join(domains[:3])}" if domains else "")
    if tool == "search_social_reactions":
        counts: Dict[str, int] = {}
        for item in r.get("results", []):
            counts[item["platform"]] = counts.get(item["platform"], 0) + 1
        parts = [f"{PLATFORM_NAMES.get(p, p)} {n}" for p, n in counts.items()]
        failed = list((r.get("errors") or {}).keys())
        text = ", ".join(parts) if parts else "nothing relevant"
        return True, text + (f" (unavailable: {', '.join(PLATFORM_NAMES.get(p, p) for p in failed)})" if failed else "")
    if tool == "execute_python_audit":
        out = _short(r.get("stdout", "") or r.get("stderr", "") or "no output", 100)
        return r.get("exit_code", 0) == 0, f"Result: {out}"
    if tool == "query_historical_claims":
        n = len(result) if isinstance(result, list) else 0
        return True, f"{n} earlier claim{'s' if n != 1 else ''} found" if n else "None yet"
    if tool == "save_video_analysis":
        return r.get("status") == "success", "Saved" if r.get("status") == "success" else "Could not save"
    return True, "Done"

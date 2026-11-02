"""youtube-mcp: MCP stdio server exposing YouTube metadata, transcripts and comments."""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from typing import Any, Dict, List, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP
from youtube_transcript_api import YouTubeTranscriptApi

import extractor

mcp = FastMCP("youtube-mcp")


@lru_cache(maxsize=1)
def _data_api() -> Any:
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY is not set; the YouTube Data API v3 is required for metadata and comments")
    return build("youtube", "v3", developerKey=key, cache_discovery=False)


@lru_cache(maxsize=1)
def _transcript_api() -> YouTubeTranscriptApi:
    return YouTubeTranscriptApi()


def _error(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, HttpError):
        return {"status": "ERROR", "message": f"YouTube Data API error {exc.resp.status}: {exc.reason}"}
    return {"status": "ERROR", "message": str(exc)}


@mcp.tool()
def get_video_metadata(video_url: str) -> Dict[str, Any]:
    """Get a video's id, title, channel, upload date, description, chapters, outbound links (affiliate/shortener/UTM flagged) and promo codes."""
    try:
        video_id = extractor.parse_video_id(video_url)
        return {"status": "OK", **extractor.fetch_metadata(video_id, _data_api())}
    except (ValueError, LookupError, RuntimeError, HttpError) as exc:
        return _error(exc)


@mcp.tool()
def get_transcript(
    video_id: str,
    languages: Optional[List[str]] = None,
    start_sec: Optional[int] = None,
    end_sec: Optional[int] = None,
) -> Dict[str, Any]:
    """Get the timestamped transcript ([MM:SS] per segment) in any available language, preferring `languages` (default English).

    Returns status TRANSCRIPTS_DISABLED when no transcript exists; callers must then abort rather than guess spoken claims.
    """
    try:
        video_id = extractor.parse_video_id(video_id)
    except ValueError as exc:
        return _error(exc)
    return extractor.fetch_transcript(video_id, languages, _transcript_api(), start_sec, end_sec)


@mcp.tool()
def get_comments(video_id: str, max_results: int = 100, sort_by: str = "top", channel_id: Optional[str] = None) -> Dict[str, Any]:
    """Get viewer comments with replies via the YouTube Data API. sort_by is 'top' or 'new'; pass channel_id to flag creator replies."""
    try:
        video_id = extractor.parse_video_id(video_id)
        return extractor.fetch_comments(video_id, max_results, sort_by, _data_api(), channel_id)
    except (ValueError, RuntimeError, HttpError) as exc:
        return _error(exc)


if __name__ == "__main__":
    print("youtube-mcp starting on stdio", file=sys.stderr)
    mcp.run(transport="stdio")

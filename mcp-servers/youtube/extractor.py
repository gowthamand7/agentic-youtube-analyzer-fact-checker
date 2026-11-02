"""Pure extraction helpers for youtube-mcp: URL parsing, metadata, transcripts, comments."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from googleapiclient.errors import HttpError
from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
)

TRANSCRIPTS_UNAVAILABLE_MESSAGE = (
    "Transcripts are disabled or unavailable for this video; cannot perform spoken claim auditing."
)

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com"}
_PATH_PREFIXES = ("shorts", "embed", "live", "v")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_TRAILING_PUNCT = ".,;:!?)]}>'\""
_CHAPTER_RE = re.compile(r"^\s*(?:[-•*]\s*)?(\d{1,2}:)?(\d{1,2}):(\d{2})\s*[-–—:|]?\s*(.+?)\s*$")
_PROMO_RE = re.compile(
    r"(?:\b(?:promo|discount|coupon|referral|affiliate)\s*(?:code)?|\bcode)\s*[:=]?\s*['\"“]?([A-Z0-9][A-Z0-9_-]{2,19})['\"”]?",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")

_AFFILIATE_DOMAINS = {"amzn.to", "amzn.com", "a.co", "geni.us", "shareasale.com", "go.skimresources.com", "howl.me", "kit.co", "shop-links.co"}
_SHORTENER_DOMAINS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "buff.ly", "is.gd", "rebrand.ly", "cutt.ly", "amzn.to", "a.co", "geni.us"}
_AFFILIATE_PARAMS = {"tag", "aff", "affid", "aff_id", "ref_", "afftrack", "irclickid", "clickid", "affiliate"}

_SORT_ORDERS = {"top": "relevance", "new": "time"}


def parse_video_id(value: str) -> str:
    """Return the 11-char video id from a YouTube URL (watch, youtu.be, shorts, embed, live) or a bare id."""
    value = (value or "").strip()
    if _VIDEO_ID_RE.match(value):
        return value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    candidate = ""
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif host in _YOUTUBE_HOSTS:
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        else:
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in _PATH_PREFIXES:
                candidate = parts[1]
    if not _VIDEO_ID_RE.match(candidate):
        raise ValueError(f"Could not extract a YouTube video id from: {value!r}")
    return candidate


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def extract_links(description: str) -> List[Dict[str, Any]]:
    """All unique outbound URLs in a description, flagged as affiliate / shortener / UTM-tagged."""
    seen: Dict[str, Dict[str, Any]] = {}
    for raw in _URL_RE.findall(description or ""):
        url = raw.rstrip(_TRAILING_PUNCT)
        if url in seen:
            continue
        parsed = urlparse(url)
        domain = (parsed.hostname or "").lower().removeprefix("www.")
        params = {k.lower() for k in parse_qs(parsed.query)}
        seen[url] = {
            "url": url,
            "domain": domain,
            "affiliate": domain in _AFFILIATE_DOMAINS or bool(params & _AFFILIATE_PARAMS),
            "shortener": domain in _SHORTENER_DOMAINS,
            "utm": any(p.startswith("utm_") for p in params),
        }
    return list(seen.values())


def extract_promo_codes(description: str) -> List[str]:
    codes: List[str] = []
    for m in _PROMO_RE.finditer(description or ""):
        code = m.group(1).upper()
        if code not in codes:
            codes.append(code)
    return codes


def parse_chapters(description: str) -> List[Dict[str, Any]]:
    """Chapter markers: description lines that begin with a timestamp."""
    chapters = []
    for line in (description or "").splitlines():
        m = _CHAPTER_RE.match(line)
        if not m:
            continue
        hours, minutes, seconds, title = m.groups()
        start = int(hours[:-1]) * 3600 if hours else 0
        start += int(minutes) * 60 + int(seconds)
        stamp = line.strip().lstrip("-•* ").split()[0]
        chapters.append({"timestamp": stamp, "start_sec": start, "title": title})
    return chapters


def _parse_duration(iso: str) -> int:
    m = _DURATION_RE.match(iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def fetch_metadata(video_id: str, client: Any) -> Dict[str, Any]:
    """Video metadata via YouTube Data API v3 videos.list."""
    resp = client.videos().list(part="snippet,contentDetails,statistics", id=video_id).execute()
    items = resp.get("items") or []
    if not items:
        raise LookupError(f"Video {video_id} not found or not accessible via the YouTube Data API")
    item = items[0]
    snippet = item.get("snippet", {})
    description = snippet.get("description", "")
    stats = item.get("statistics", {})
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": snippet.get("title", ""),
        "channel_id": snippet.get("channelId", ""),
        "channel_name": snippet.get("channelTitle", ""),
        "upload_date": snippet.get("publishedAt", ""),
        "language": snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage") or "",
        "duration_sec": _parse_duration(item.get("contentDetails", {}).get("duration", "")),
        "view_count": int(stats.get("viewCount", 0) or 0),
        "comment_count": int(stats.get("commentCount", 0) or 0),
        "description": description,
        "chapters": parse_chapters(description),
        "links": extract_links(description),
        "promo_codes": extract_promo_codes(description),
    }


def _segments(fetched: Any, start_sec: Optional[int], end_sec: Optional[int]) -> List[Dict[str, Any]]:
    out = []
    for sn in fetched.snippets:
        if start_sec is not None and sn.start < start_sec:
            continue
        if end_sec is not None and sn.start > end_sec:
            continue
        out.append({"timestamp": format_timestamp(sn.start), "start_sec": sn.start, "text": sn.text})
    return out


def _transcript_ok(fetched: Any, start_sec: Optional[int], end_sec: Optional[int]) -> Dict[str, Any]:
    return {
        "status": "OK",
        "language": fetched.language_code,
        "is_generated": fetched.is_generated,
        "segments": _segments(fetched, start_sec, end_sec),
    }


def _transcript_failure(status: str, message: str) -> Dict[str, Any]:
    return {"status": status, "message": message, "segments": []}


def fetch_transcript(
    video_id: str,
    languages: Optional[List[str]],
    api: Any,
    start_sec: Optional[int] = None,
    end_sec: Optional[int] = None,
) -> Dict[str, Any]:
    """Timestamped transcript. Tries the preferred languages, then any available transcript.

    Failures are returned as a status (TRANSCRIPTS_DISABLED, VIDEO_UNAVAILABLE, REQUEST_BLOCKED,
    TRANSCRIPT_ERROR) so the agent can abort cleanly instead of raising.
    """
    preferred = languages or ["en"]
    try:
        try:
            return _transcript_ok(api.fetch(video_id, languages=preferred), start_sec, end_sec)
        except NoTranscriptFound:
            for transcript in api.list(video_id):
                return _transcript_ok(transcript.fetch(), start_sec, end_sec)
            raise
    except (TranscriptsDisabled, NoTranscriptFound):
        return _transcript_failure("TRANSCRIPTS_DISABLED", TRANSCRIPTS_UNAVAILABLE_MESSAGE)
    except VideoUnavailable:
        return _transcript_failure("VIDEO_UNAVAILABLE", "Video is unavailable (private, removed, or region-locked).")
    except (IpBlocked, RequestBlocked):
        return _transcript_failure("REQUEST_BLOCKED", "YouTube blocked the transcript request from this IP address.")
    except CouldNotRetrieveTranscript as exc:
        return _transcript_failure("TRANSCRIPT_ERROR", f"Could not retrieve transcript: {type(exc).__name__}")


def _comment(snippet: Dict[str, Any], channel_id: Optional[str]) -> Dict[str, Any]:
    author_channel = (snippet.get("authorChannelId") or {}).get("value", "")
    return {
        "author": snippet.get("authorDisplayName", ""),
        "text": snippet.get("textDisplay", ""),
        "likes": int(snippet.get("likeCount", 0) or 0),
        "published_at": snippet.get("publishedAt", ""),
        "is_creator": bool(channel_id) and author_channel == channel_id,
    }


def _http_error_reason(exc: HttpError) -> str:
    try:
        return json.loads(exc.content)["error"]["errors"][0]["reason"]
    except (ValueError, KeyError, IndexError, TypeError):
        return ""


def fetch_comments(
    video_id: str,
    max_results: int,
    sort_by: str,
    client: Any,
    channel_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Top-level comments (with replies) via YouTube Data API v3 commentThreads.list."""
    if sort_by not in _SORT_ORDERS:
        raise ValueError(f"sort_by must be one of {sorted(_SORT_ORDERS)}")
    max_results = max(1, min(int(max_results), 500))
    comments: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    try:
        while len(comments) < max_results:
            resp = (
                client.commentThreads()
                .list(
                    part="snippet,replies",
                    videoId=video_id,
                    maxResults=min(100, max_results - len(comments)),
                    order=_SORT_ORDERS[sort_by],
                    textFormat="plainText",
                    pageToken=page_token,
                )
                .execute()
            )
            for thread in resp.get("items", []):
                top = _comment(thread["snippet"]["topLevelComment"]["snippet"], channel_id)
                top["reply_count"] = thread["snippet"].get("totalReplyCount", 0)
                top["replies"] = [_comment(r["snippet"], channel_id) for r in thread.get("replies", {}).get("comments", [])]
                comments.append(top)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except HttpError as exc:
        reason = _http_error_reason(exc)
        if reason == "commentsDisabled":
            return {"status": "COMMENTS_DISABLED", "comments": []}
        raise
    return {"status": "OK", "comments": comments[:max_results]}

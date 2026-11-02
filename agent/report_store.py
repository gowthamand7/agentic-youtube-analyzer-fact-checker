"""Persist finished reports on disk so a video is analyzed once and re-shown until the user asks to re-run."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "reports"
_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}

# State keys needed to re-render a report exactly as it was first shown.
STORED_KEYS = (
    "video_id", "final_report_md", "overall_accuracy_score", "saved_to_memory", "verification_results", "community_insights",
)


def video_id_from_url(url: str) -> Optional[str]:
    """11-char video id from a watch / youtu.be / shorts / embed / live URL, or None."""
    parsed = urlparse((url or "").strip())
    if (parsed.hostname or "").lower() not in _HOSTS:
        return None
    if parsed.hostname == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif parsed.path == "/watch":
        candidate = parse_qs(parsed.query).get("v", [""])[0]
    else:
        parts = [p for p in parsed.path.split("/") if p]
        candidate = parts[1] if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v") else ""
    return candidate if _ID.fullmatch(candidate) else None


def _directory() -> Path:
    return Path(os.environ.get("REPORT_DIR") or DEFAULT_DIR)


def _path(video_id: str) -> Path:
    if not _ID.fullmatch(video_id):  # also blocks path traversal
        raise ValueError(f"invalid video id: {video_id!r}")
    return _directory() / f"{video_id}.json"


def save_report(state: Dict[str, Any], video: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    """Store a finished report. Aborted runs (error_message set) are never stored. Returns the file or None."""
    video_id = state.get("video_id")
    if state.get("error_message") or not video_id or not state.get("final_report_md"):
        return None
    record = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "video": video or {},
        "state": {k: state[k] for k in STORED_KEYS if k in state},
    }
    try:
        path = _path(video_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False)
        os.replace(tmp, path)
        return path
    except (OSError, ValueError):
        return None


def load_report(video_id: str) -> Optional[Dict[str, Any]]:
    """Return {"generated_at", "video", "state"} or None when missing/unreadable."""
    try:
        record = json.loads(_path(video_id).read_text(encoding="utf-8"))
        return record if "state" in record and "generated_at" in record else None
    except (OSError, ValueError):
        return None

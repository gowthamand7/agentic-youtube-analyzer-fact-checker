"""Supadata transcript fallback, used only when the scraper (youtube-transcript-api) is blocked or errors.

Several API keys can be supplied (SUPADATA_API_KEYS, comma separated). A key that is out of quota or rejected is
skipped for a cooldown period and the next one is tried.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional

import requests

API_URL = "https://api.supadata.ai/v1/transcript"
KEY_COOLDOWN_SEC = 3600
REQUEST_TIMEOUT_SEC = 30
_SKIP_KEY_STATUSES = {401, 402, 403, 429}  # bad key, plan limit, quota/rate limit: try the next key


def keys_from_env() -> List[str]:
    raw = os.environ.get("SUPADATA_API_KEYS") or os.environ.get("SUPADATA_API_KEY") or ""
    return [k.strip() for k in raw.split(",") if k.strip()]


class SupadataFallback:
    def __init__(
        self,
        keys: List[str],
        http_get: Callable[..., Any] = requests.get,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._keys = keys
        self._get = http_get
        self._clock = clock
        self._cooldown_until: Dict[str, float] = {}
        self._next = 0

    def _usable_keys(self) -> List[str]:
        """Keys not cooling down, starting at a rotating index so load spreads across accounts."""
        now = self._clock()
        n = len(self._keys)
        ordered = [self._keys[(self._next + i) % n] for i in range(n)]
        return [k for k in ordered if self._cooldown_until.get(k, 0) <= now]

    def fetch(self, video_id: str, languages: Optional[List[str]]) -> Dict[str, Any]:
        """Same result shape as extractor.fetch_transcript. Returns status NO_FALLBACK when it cannot help."""
        keys = self._usable_keys()
        if not keys:
            return {"status": "NO_FALLBACK", "message": "No Supadata key available (none configured or all out of quota).", "segments": []}
        params: Dict[str, Any] = {"url": f"https://www.youtube.com/watch?v={video_id}", "mode": "native", "text": "false"}
        if languages:
            params["lang"] = languages[0]
        last = "Supadata request failed"
        for key in keys:
            try:
                resp = self._get(API_URL, params=params, headers={"x-api-key": key}, timeout=REQUEST_TIMEOUT_SEC)
            except requests.RequestException as exc:
                last = f"Supadata unreachable: {type(exc).__name__}"
                continue
            if resp.status_code in _SKIP_KEY_STATUSES:
                self._cooldown_until[key] = self._clock() + KEY_COOLDOWN_SEC
                last = f"Supadata key rejected or out of quota (HTTP {resp.status_code})"
                continue
            self._next = (self._keys.index(key) + 1) % len(self._keys)
            return self._parse(resp)
        return {"status": "NO_FALLBACK", "message": last, "segments": []}

    @staticmethod
    def _parse(resp: Any) -> Dict[str, Any]:
        if resp.status_code == 206 or resp.status_code == 404:
            return {"status": "TRANSCRIPTS_DISABLED", "message": "No transcript is available for this video.", "segments": []}
        if resp.status_code != 200:
            return {"status": "NO_FALLBACK", "message": f"Supadata returned HTTP {resp.status_code}", "segments": []}
        try:
            body = resp.json()
            chunks = body["content"]
            segments = [
                {"timestamp": _stamp(c["offset"] / 1000), "start_sec": c["offset"] / 1000, "text": c["text"]} for c in chunks
            ]
        except (ValueError, KeyError, TypeError):
            return {"status": "NO_FALLBACK", "message": "Supadata returned an unexpected response.", "segments": []}
        if not segments:
            return {"status": "TRANSCRIPTS_DISABLED", "message": "No transcript is available for this video.", "segments": []}
        return {"status": "OK", "language": body.get("lang", ""), "is_generated": None, "segments": segments, "source": "supadata"}


def _stamp(seconds: float) -> str:
    # imported lazily to keep this module free of a circular import with extractor
    from extractor import format_timestamp

    return format_timestamp(seconds)


def default_fallback() -> Optional[SupadataFallback]:
    keys = keys_from_env()
    return SupadataFallback(keys) if keys else None

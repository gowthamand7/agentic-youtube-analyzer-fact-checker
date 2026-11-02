"""Small on-disk JSON cache with per-entry TTL, used to avoid re-hitting YouTube for the same video."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

_SAFE_KEY = re.compile(r"[^A-Za-z0-9_.-]")


class DiskCache:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _path(self, key: str) -> Path:
        return self.directory / (_SAFE_KEY.sub("_", key) + ".json")

    def get(self, key: str) -> Optional[Any]:
        """Return the cached value, or None when missing, expired or unreadable (a corrupt file is a miss)."""
        try:
            entry = json.loads(self._path(key).read_text(encoding="utf-8"))
            if time.time() - entry["stored_at"] > entry["ttl_sec"]:
                return None
            return entry["value"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, key: str, value: Any, ttl_sec: float) -> None:
        """Write atomically so a concurrent reader never sees a half-written file. Failures are non-fatal."""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"stored_at": time.time(), "ttl_sec": ttl_sec, "value": value}, fh, ensure_ascii=False)
            os.replace(tmp, self._path(key))
        except OSError:
            pass


def default_cache() -> DiskCache:
    root = Path(__file__).resolve().parent.parent.parent
    return DiskCache(Path(os.environ.get("CACHE_DIR") or root / "cache"))

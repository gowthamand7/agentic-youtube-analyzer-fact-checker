"""The user's knowledge profile: things they marked "I already know this". Stored locally as JSON.

Single local user for now (see docs/ROADMAP.md for the hosted multi-user plan); the file path is the only
thing that would change per user.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "profile" / "knowledge.json"
_lock = threading.Lock()


def normalize_concept(concept: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (concept or "").lower()).strip()


class KnowledgeStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or os.environ.get("KNOWLEDGE_FILE") or DEFAULT_FILE)

    def _read(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [e for e in data.get("entries", []) if isinstance(e, dict) and "id" in e]
        except (OSError, ValueError, AttributeError):
            return []

    def _write(self, entries: List[Dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"entries": entries}, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written profile

    def entries(self) -> List[Dict[str, Any]]:
        with _lock:
            return self._read()

    def version(self) -> str:
        """Changes whenever the set of known entries changes; used to invalidate cached personalization."""
        return hashlib.sha1("|".join(sorted(e["id"] for e in self.entries())).encode()).hexdigest()[:12]

    def add(self, concept: str, text: str, tags: List[str], video_id: str, video_title: str = "") -> Dict[str, Any]:
        """Mark something as known. Marking the same concept twice returns the existing entry."""
        key = normalize_concept(concept) or normalize_concept(text)
        entry_id = hashlib.sha1(key.encode()).hexdigest()[:10]
        with _lock:
            entries = self._read()
            existing = next((e for e in entries if e["id"] == entry_id), None)
            if existing:
                return existing
            entry = {
                "id": entry_id, "concept": concept, "text": text, "tags": tags, "video_id": video_id,
                "video_title": video_title, "marked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self._write(entries + [entry])
            return entry

    def remove(self, entry_id: str) -> bool:
        with _lock:
            entries = self._read()
            kept = [e for e in entries if e["id"] != entry_id]
            if len(kept) == len(entries):
                return False
            self._write(kept)
            return True

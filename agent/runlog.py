"""Per-request debug log: one JSONL file per analysis, plus a console one-liner per event.

Each line: {"ts", "t_ms" (since request start), "rid", "event", ...fields}. View with scripts/show_log.py.
Set LOG_DIR to change the directory (default ./logs) and LOG_PROMPTS=1 to also record full LLM prompts.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("yta.run")

DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
MAX_FIELD_CHARS = 3000

_current: ContextVar[Optional["RunLog"]] = ContextVar("yta_run_log", default=None)


def clip(value: Any, limit: int = MAX_FIELD_CHARS) -> Any:
    """Make a value JSON-safe and bounded so one huge tool result cannot bloat the log."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f"... [+{len(text) - limit} chars]"


def clip_obj(value: Any, limit: int = MAX_FIELD_CHARS) -> Any:
    """Keep structured values (tool arguments) as objects so parameter names stay visible; clip only when huge."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= limit else clip(text, limit)


def log_prompts_enabled() -> bool:
    return os.environ.get("LOG_PROMPTS") == "1"


class RunLog:
    def __init__(self, video_url: str, log_dir: Optional[Path] = None) -> None:
        self.rid = uuid.uuid4().hex[:8]
        self._t0 = time.monotonic()
        directory = Path(log_dir or os.environ.get("LOG_DIR") or DEFAULT_LOG_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.path = directory / f"{stamp}_{self.rid}.jsonl"
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)  # line-buffered: readable while running
        self._pending: List[Dict[str, Any]] = []  # records not yet streamed to the developer trace tab
        self.write("request_start", video_url=video_url)

    def drain(self) -> List[Dict[str, Any]]:
        """Return and clear the records written since the last call (for the live developer trace)."""
        records, self._pending = self._pending, []
        return records

    def write(self, event: str, **fields: Any) -> None:
        record: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "t_ms": round((time.monotonic() - self._t0) * 1000),
            "rid": self.rid,
            "event": event,
            **fields,
        }
        if not self._fh.closed:
            self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._pending.append(record)
        log.info("[%s] %s", self.rid, _console_line(event, fields))

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()


def _console_line(event: str, fields: Dict[str, Any]) -> str:
    brief = {k: v for k, v in fields.items() if k in ("tool", "node", "title", "ok", "duration_ms", "purpose", "status", "error", "video_url", "score")}
    return event + ("  " + " ".join(f"{k}={v}" for k, v in brief.items()) if brief else "")


def start_run(video_url: str) -> RunLog:
    run = RunLog(video_url)
    _current.set(run)
    return run


def end_run(run: RunLog) -> None:
    run.close()
    _current.set(None)


def log_event(event: str, **fields: Any) -> None:
    """Record an event on the current request's log; no-op outside a request."""
    run = _current.get()
    if run is not None:
        run.write(event, **fields)


def current_rid() -> Optional[str]:
    run = _current.get()
    return run.rid if run else None

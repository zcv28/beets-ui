"""Background check of a download source: is it connected, what is downloading,
and which downloads just finished.

The sidebar and the inbox read ``status`` and ``rows`` (so pages never wait on the
source), live updates read ``version``, and the jobs panel turns ``events`` into
pop-ups.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque
from typing import Any

from .base import Source, SourceError

log = logging.getLogger(__name__)


class Watcher:
    def __init__(self, source: Source, interval: float = 5):
        self.source = source
        self.interval = interval
        self.status: dict[str, Any] | None = None
        self.rows: list[dict[str, Any]] = []
        # Changes whenever the connection or any download changes.
        self.version = ""
        self.events: deque[dict[str, Any]] = deque(maxlen=50)
        self._states: dict[tuple[str, str], str] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def down(self) -> bool:
        return bool(self.status) and not self.status["connected"]

    def poll(self) -> None:
        with self._lock:
            self._poll()

    def _poll(self) -> None:
        try:
            self.status = self.source.status()
        except SourceError as e:
            self.status = {"reachable": False, "connected": False, "error": str(e)}
            self.version = "down"
            return
        try:
            rows = self.source.transfers()
        except SourceError:
            return
        now = time.time()
        if self._states is not None:
            for r in rows:
                before = self._states.get((r["origin"], r["folder"]))
                if before in ("downloading", "queued") and r["state"] in ("done", "failed"):
                    self.events.append({**r, "at": now, "ok": r["state"] == "done"})
        self._states = {(r["origin"], r["folder"]): r["state"] for r in rows}
        self.rows = rows
        seen = [self.status["connected"]] + [
            (r["origin"], r["folder"], r["state"], r["percent"], r["place"]) for r in rows
        ]
        self.version = hashlib.md5(repr(seen).encode()).hexdigest()[:12]

    def events_since(self, ts: float) -> list[dict[str, Any]]:
        return [e for e in list(self.events) if e["at"] > ts]

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:  # never let the thread die
                log.exception("source watcher")
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="beets-ui-source", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

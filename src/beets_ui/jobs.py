"""A single background worker thread that runs every write operation.

Running imports, edits, removals and console commands one at a time keeps
memory usage flat and avoids SQLite lock contention on the beets library.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

from .state import State

log = logging.getLogger("beets_ui.jobs")


class JobContext:
    """Passed to job handlers; collects log output for the job."""

    def __init__(self, state: State, job_id: int, params: dict[str, Any]):
        self.state = state
        self.job_id = job_id
        self.params = params
        self._buf: list[str] = []
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()

    def log(self, line: str) -> None:
        with self._lock:
            self._buf.append(line.rstrip("\n") + "\n")
            if time.monotonic() - self._last_flush > 0.5 or len(self._buf) > 200:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._buf:
            self.state.append_log(self.job_id, "".join(self._buf))
            self._buf.clear()
        self._last_flush = time.monotonic()


class JobLogHandler(logging.Handler):
    """Routes `beets` log records into the running job's log."""

    def __init__(self, ctx: JobContext):
        super().__init__(level=logging.INFO)
        self.ctx = ctx

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed log args
            msg = str(record.msg)
        prefix = "" if record.levelno <= logging.INFO else f"[{record.levelname.lower()}] "
        self.ctx.log(prefix + msg)


Handler = Callable[[JobContext], None]


class JobQueue:
    def __init__(self, state: State):
        self.state = state
        self.handlers: dict[str, Handler] = {}
        self._q: queue.Queue[int | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self.current: int | None = None
        self.on_finish: list[Callable[[dict[str, Any]], None]] = []

    def register(self, kind: str) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            self.handlers[kind] = fn
            return fn

        return deco

    def submit(self, kind: str, title: str, params: dict | None = None, user: str | None = None):
        if kind not in self.handlers:
            raise KeyError(f"unknown job kind {kind}")
        job_id = self.state.create_job(kind, title, params or {}, user)
        self._q.put(job_id)
        return job_id

    def pending(self) -> int:
        return self._q.qsize()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="beets-ui-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._q.put(None)

    def run_pending_sync(self) -> None:
        """Run all queued jobs on the calling thread (used by tests)."""
        while True:
            try:
                job_id = self._q.get_nowait()
            except queue.Empty:
                return
            if job_id is None:
                return
            self._run(job_id)

    def _loop(self) -> None:
        while True:
            job_id = self._q.get()
            if job_id is None:
                return
            self._run(job_id)

    def _run(self, job_id: int) -> None:
        job = self.state.job(job_id)
        if job is None or job["status"] == "cancelled":
            return
        ctx = JobContext(self.state, job_id, job["params"])
        handler = self.handlers[job["kind"]]
        self.current = job_id
        self.state.set_job(job_id, status="running", started_at=time.time())
        beets_logger = logging.getLogger("beets")
        log_handler = JobLogHandler(ctx)
        beets_logger.addHandler(log_handler)
        status, error = "done", None
        try:
            handler(ctx)
        except Exception as e:  # noqa: BLE001 - report every failure to the UI
            status, error = "failed", f"{type(e).__name__}: {e}"
            ctx.log(traceback.format_exc())
            log.exception("job %s failed", job_id)
        finally:
            beets_logger.removeHandler(log_handler)
            ctx.flush()
            self.current = None
            self.state.set_job(job_id, status=status, error=error, finished_at=time.time())
            done = self.state.job(job_id)
            for cb in self.on_finish:
                try:
                    cb(done)  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001
                    log.exception("on_finish callback failed")
            self.state.prune_jobs()

    def cancel(self, job_id: int) -> bool:
        job = self.state.job(job_id)
        if job and job["status"] == "queued":
            self.state.set_job(job_id, status="cancelled", finished_at=time.time())
            return True
        return False

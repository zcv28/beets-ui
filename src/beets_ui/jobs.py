"""Background worker threads for jobs.

Every job that changes the library (imports, edits, removals, console
commands) runs on the one ``main`` worker, one at a time: that keeps memory
use flat and avoids SQLite lock contention on the beets library. Jobs that
only read the library and wait on the network (discography lookups, the
Jellyfin sync) run on a second ``net`` worker so they never hold up an
import.
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
        # Set by a handler that finished but left something for you to look at;
        # the job still ends "done", with this as its note.
        self.note: str | None = None
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
        self.thread = threading.get_ident()

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self.thread:
            return  # logged by a job on the other worker
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed log args
            msg = str(record.msg)
        prefix = "" if record.levelno <= logging.INFO else f"[{record.levelname.lower()}] "
        self.ctx.log(prefix + msg)


Handler = Callable[[JobContext], None]


LANES = ("main", "net")


class JobQueue:
    def __init__(self, state: State):
        self.state = state
        self.handlers: dict[str, Handler] = {}
        self.lanes: dict[str, str] = {}
        self._qs: dict[str, queue.Queue[int | None]] = {lane: queue.Queue() for lane in LANES}
        self._threads: dict[str, threading.Thread] = {}
        self._running: dict[str, int] = {}
        self.on_finish: list[Callable[[dict[str, Any]], None]] = []

    def register(self, kind: str, lane: str = "main") -> Callable[[Handler], Handler]:
        if lane not in LANES:
            raise ValueError(f"unknown job lane {lane}")

        def deco(fn: Handler) -> Handler:
            self.handlers[kind] = fn
            self.lanes[kind] = lane
            return fn

        return deco

    def submit(self, kind: str, title: str, params: dict | None = None, user: str | None = None):
        if kind not in self.handlers:
            raise KeyError(f"unknown job kind {kind}")
        job_id = self.state.create_job(kind, title, params or {}, user)
        self._qs[self.lanes[kind]].put(job_id)
        return job_id

    @property
    def current(self) -> int | None:
        """A running job (the main worker's first), or None."""
        return self._running.get("main") or self._running.get("net")

    def running(self) -> list[int]:
        return [j for j in (self._running.get(lane) for lane in LANES) if j]

    def pending(self) -> int:
        return sum(q.qsize() for q in self._qs.values())

    def start(self) -> None:
        for lane in LANES:
            t = self._threads.get(lane)
            if t and t.is_alive():
                continue
            t = threading.Thread(
                target=self._loop, args=(lane,), name=f"beets-ui-jobs-{lane}", daemon=True
            )
            self._threads[lane] = t
            t.start()

    def stop(self) -> None:
        for q in self._qs.values():
            q.put(None)

    def run_pending_sync(self) -> None:
        """Run all queued jobs on the calling thread (used by tests)."""
        for q in self._qs.values():
            while True:
                try:
                    job_id = q.get_nowait()
                except queue.Empty:
                    break
                if job_id is None:
                    break
                self._run(job_id)

    def _loop(self, lane: str) -> None:
        q = self._qs[lane]
        while True:
            job_id = q.get()
            if job_id is None:
                return
            self._run(job_id)

    def _run(self, job_id: int) -> None:
        job = self.state.job(job_id)
        if job is None or job["status"] == "cancelled":
            return
        ctx = JobContext(self.state, job_id, job["params"])
        handler = self.handlers[job["kind"]]
        lane = self.lanes[job["kind"]]
        self._running[lane] = job_id
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
            self._running.pop(lane, None)
            if status == "done" and ctx.note:
                error = ctx.note
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

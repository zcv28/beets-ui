"""The review inbox: folder discovery, status tracking and import jobs."""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from . import importer
from .jobs import JobContext, JobQueue
from .settings import Settings
from .state import State

log = logging.getLogger("beets_ui.inbox")

AUDIO_EXT = {
    ".mp3",
    ".flac",
    ".m4a",
    ".mp4",
    ".aac",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".wv",
    ".ape",
    ".aiff",
    ".aif",
    ".alac",
    ".wma",
    ".mpc",
    ".dsf",
    ".dff",
}

STATUS_LABELS = {
    "new": "New",
    "queued": "Queued",
    "previewing": "Looking up",
    "ready": "Ready",
    "attention": "Needs attention",
    "duplicate": "Duplicate",
    "importing": "Importing",
    "imported": "Imported",
    "failed": "Failed",
}


def folder_signature(path: Path) -> tuple[str, int]:
    """Cheap change detector: number of files, total size, newest mtime."""
    count = size = 0
    newest = 0.0
    audio = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith("."):
                continue
            try:
                st = os.stat(os.path.join(root, f))
            except OSError:
                continue
            count += 1
            size += st.st_size
            newest = max(newest, st.st_mtime)
            if os.path.splitext(f)[1].lower() in AUDIO_EXT:
                audio += 1
    return f"{count}:{size}:{int(newest)}", audio


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class Inbox:
    def __init__(self, settings: Settings, state: State, jobs: JobQueue):
        self.settings = settings
        self.state = state
        self.jobs = jobs
        self.root = settings.inbox
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._scan_lock = threading.Lock()
        jobs.register("preview")(self._job_preview)
        jobs.register("import")(self._job_import)

    # --------------------------------------------------------------- scanning

    def resolve(self, rel: str) -> Path:
        """Map a folder id (path relative to the inbox) to an absolute path."""
        p = (self.root / rel).resolve()
        if not is_within(p, self.root) or p == self.root.resolve():
            raise ValueError("path outside inbox")
        return p

    def scan(self) -> None:
        with self._scan_lock:
            self._scan()

    def _scan(self) -> None:
        if not self.root.is_dir():
            log.warning("inbox %s does not exist", self.root)
            return
        now = time.time()
        seen = set()
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            rel = entry.name
            seen.add(rel)
            sig, _ = folder_signature(entry)
            row = self.state.folder(rel)
            if row is None:
                self.state.upsert_folder(rel, status="new", signature=sig, changed_at=now)
                continue
            if row["signature"] != sig and row["status"] not in (
                "queued",
                "previewing",
                "importing",
            ):
                # Contents changed (e.g. slskd is still downloading): previous
                # lookup results are stale, wait for the folder to settle again.
                self.state.upsert_folder(
                    rel,
                    status="new",
                    signature=sig,
                    changed_at=now,
                    tasks=[],
                    best=None,
                    duplicates={},
                    error=None,
                )
        for row in self.state.folders():
            if row["path"] not in seen and row["status"] not in ("previewing", "importing"):
                self.state.delete_folder(row["path"])
        if self.settings.auto_preview:
            for row in self.state.folders():
                if row["status"] == "new" and now - row["changed_at"] >= self.settings.debounce:
                    self.queue_preview(row["path"])

    def start_watcher(self) -> None:
        self._thread = threading.Thread(target=self._watch, name="beets-ui-inbox", daemon=True)
        self._thread.start()

    def stop_watcher(self) -> None:
        self._stop.set()

    def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception:  # noqa: BLE001
                log.exception("inbox scan failed")
            self._stop.wait(self.settings.scan_interval)

    # ----------------------------------------------------------------- queue

    def queue_preview(
        self, rel: str, mode: str | None = None, user: str | None = None, **search: Any
    ) -> int:
        row = self.state.folder(rel) or {}
        mode = mode or row.get("mode") or "albums"
        job_id = self.jobs.submit(
            "preview", f"Look up {rel}", {"folder": rel, "mode": mode, **search}, user
        )
        self.state.upsert_folder(rel, status="queued", mode=mode, job_id=job_id, error=None)
        return job_id

    def queue_import(self, rel: str, decisions: dict[str, Any], user: str | None = None) -> int:
        row = self.state.folder(rel)
        if row is None:
            raise KeyError(rel)
        job_id = self.jobs.submit(
            "import", f"Import {rel}", {"folder": rel, "mode": row["mode"]}, user
        )
        self.state.upsert_folder(
            rel, status="queued", decisions=decisions, job_id=job_id, error=None
        )
        return job_id

    def delete_source(self, rel: str) -> None:
        shutil.rmtree(self.resolve(rel))
        self.state.delete_folder(rel)

    # ------------------------------------------------------------------ jobs

    def _job_preview(self, ctx: JobContext) -> None:
        rel = ctx.params["folder"]
        mode = ctx.params.get("mode", "albums")
        path = self.resolve(rel)
        self.state.upsert_folder(rel, status="previewing")
        ctx.log(f"Looking up candidates for {rel} ({mode})")
        try:
            tasks = importer.preview_folder(
                str(path),
                mode=mode,
                search_artist=ctx.params.get("artist"),
                search_name=ctx.params.get("album"),
                search_ids=ctx.params.get("ids"),
            )
        except Exception as e:
            self.state.upsert_folder(rel, status="failed", error=str(e))
            raise
        sig, _ = folder_signature(path)
        old = self.state.folder(rel) or {}
        if ctx.params.get("merge") and old.get("tasks"):
            tasks = _merge_candidates(old["tasks"], tasks)
        best = min((t["best"] for t in tasks if t["best"] is not None), default=None)
        if not tasks:
            status, err = "attention", "No music files found"
        elif any(not t["candidates"] for t in tasks):
            status, err = "attention", "No candidates found for at least one album"
        else:
            status, err = "ready", None
        self.state.upsert_folder(
            rel,
            status=status,
            tasks=tasks,
            best=best,
            error=err,
            signature=sig,
            duplicates={},
            decisions=_default_decisions(tasks),
        )
        for t in tasks:
            ctx.log(
                f"  {t['cur_artist']} - {t['cur_album']}: {len(t['candidates'])} candidates"
                + (f", best {t['best']}%" if t["best"] is not None else "")
            )

    def _job_import(self, ctx: JobContext) -> None:
        rel = ctx.params["folder"]
        row = self.state.folder(rel)
        if row is None:
            raise KeyError(rel)
        path = self.resolve(rel)
        self.state.upsert_folder(rel, status="importing")
        try:
            outcomes = importer.apply_folder(str(path), row["decisions"], row["mode"])
        except Exception as e:
            self.state.upsert_folder(rel, status="failed", error=str(e))
            raise
        dups: dict[str, Any] = {}
        problems = []
        for key, o in outcomes.items():
            ctx.log(f"{o.result}: {key.splitlines()[0]} {o.detail}")
            if o.result == "duplicate":
                dups[key] = {"existing": o.duplicates, "current": o.current}
            elif o.result == "missing-candidate":
                problems.append(o.detail)
        imported = [o for o in outcomes.values() if o.result == "imported"]
        if dups:
            self.state.upsert_folder(
                rel,
                status="duplicate",
                duplicates=dups,
                error="Already in library: choose how to resolve the duplicate",
            )
            return
        if problems:
            self.state.upsert_folder(rel, status="attention", error="; ".join(problems))
            return
        if not imported:
            self.state.upsert_folder(rel, status="ready", error="Nothing was imported")
            return
        if path.exists() and self.settings.delete_after_import:
            ctx.log(f"Deleting source folder {path}")
            shutil.rmtree(path, ignore_errors=True)
            self.state.delete_folder(rel)
            return
        sig, _ = folder_signature(path) if path.exists() else ("", 0)
        self.state.upsert_folder(rel, status="imported", signature=sig, error=None, duplicates={})


def _default_decisions(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for t in tasks:
        if t["candidates"]:
            c = t["candidates"][0]
            out[t["key"]] = {"action": "apply", "id": c["id"], "source": c["source"]}
        else:
            out[t["key"]] = {"action": "skip"}
    return out


def _merge_candidates(old: list[dict], new: list[dict]) -> list[dict]:
    """Add newly searched candidates to the existing task list (dedup by id)."""
    by_key = {t["key"]: t for t in old}
    for t in new:
        prev = by_key.get(t["key"])
        if prev is None:
            by_key[t["key"]] = t
            continue
        seen = {(c["source"], c["id"]) for c in t["candidates"]}
        t["candidates"] = t["candidates"] + [
            c for c in prev["candidates"] if (c["source"], c["id"]) not in seen
        ]
        t["candidates"].sort(key=lambda c: c["distance"])
        t["best"] = t["candidates"][0]["match"] if t["candidates"] else None
        by_key[t["key"]] = t
    return list(by_key.values())

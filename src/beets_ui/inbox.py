"""The review inbox: folder discovery, status tracking and import jobs."""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from . import acquire, importer, romanize
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
    "new": "Not looked up",
    "queued": "Queued",
    "previewing": "Looking up",
    "ready": "Ready",
    "attention": "Needs attention",
    "duplicate": "Duplicate",
    "importing": "Importing",
    "imported": "Imported",
    "failed": "Failed",
}


MAX_LISTED_FILES = 500
MAX_TAGGED_FILES = 60


def scan_folder(path: Path) -> tuple[str, list[dict[str, Any]]]:
    """Return a cheap change signature and the folder's file listing."""
    count = size = 0
    newest = 0.0
    files: list[dict[str, Any]] = []
    for root, dirs, names in os.walk(path):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for f in sorted(names):
            if f.startswith("."):
                continue
            full = os.path.join(root, f)
            try:
                st = os.stat(full)
            except OSError:
                continue
            count += 1
            size += st.st_size
            newest = max(newest, st.st_mtime)
            if len(files) < MAX_LISTED_FILES:
                files.append(
                    {
                        "path": os.path.relpath(full, path),
                        "size": st.st_size,
                        "audio": os.path.splitext(f)[1].lower() in AUDIO_EXT,
                    }
                )
    return f"{count}:{size}:{int(newest)}", files


def folder_signature(path: Path) -> tuple[str, int]:
    sig, files = scan_folder(path)
    return sig, sum(1 for f in files if f["audio"])


def read_tag_keys(path: Path, files: list[dict[str, Any]]) -> dict[str, Any]:
    """Tags used to recognise a folder that is already in the library."""
    from mediafile import MediaFile

    ids: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for f in [f for f in files if f["audio"]][:MAX_TAGGED_FILES]:
        try:
            mf = MediaFile(str(path / f["path"]))
        except Exception:  # noqa: BLE001 - unreadable/unsupported file
            continue
        if mf.mb_albumid:
            ids.add(mf.mb_albumid)
        artist = mf.albumartist or mf.artist
        if artist and mf.album:
            pairs.add((artist, mf.album))
    return {"ids": sorted(ids), "pairs": sorted(pairs)}


def library_matches(tagkeys: dict[str, Any], tasks: list[dict[str, Any]]) -> list[dict]:
    """Library albums that this inbox folder appears to contain already.

    Matches on MusicBrainz album IDs (from the file tags or a confident lookup
    candidate) and on album artist + album name, ignoring case.
    """
    from . import beets_ctx

    ids = {i: "MusicBrainz ID in tags" for i in tagkeys.get("ids", [])}
    for t in tasks:
        cands = t.get("candidates") or []
        if cands and cands[0].get("id") and (cands[0].get("match") or 0) >= 90:
            ids.setdefault(cands[0]["id"], "best lookup match")
    found: dict[int, dict[str, Any]] = {}
    lib = beets_ctx.lib()
    with lib.transaction() as tx:
        for mbid, how in ids.items():
            for row in tx.query(
                "SELECT id, albumartist, album FROM albums WHERE mb_albumid = ?", (mbid,)
            ):
                found.setdefault(
                    row[0], {"id": row[0], "albumartist": row[1], "album": row[2], "how": how}
                )
        for artist, album in tagkeys.get("pairs", []):
            for row in tx.query(
                "SELECT id, albumartist, album FROM albums "
                "WHERE lower(albumartist) = lower(?) AND lower(album) = lower(?)",
                (artist, album),
            ):
                found.setdefault(
                    row[0],
                    {
                        "id": row[0],
                        "albumartist": row[1],
                        "album": row[2],
                        "how": "same artist and album name",
                    },
                )
    return list(found.values())


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
            sig, files = scan_folder(entry)
            row = self.state.folder(rel)
            if row is None:
                self.state.upsert_folder(
                    rel,
                    status="new",
                    signature=sig,
                    changed_at=now,
                    files=files,
                    tagkeys=read_tag_keys(entry, files),
                )
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
                    files=files,
                    tagkeys=read_tag_keys(entry, files),
                )
            elif not row["files"] and files:
                # Folder recorded before file listings existed.
                self.state.upsert_folder(rel, files=files, tagkeys=read_tag_keys(entry, files))
        for row in self.state.folders():
            if row["path"] not in seen and row["status"] not in ("previewing", "importing"):
                self.state.delete_folder(row["path"])
        self.refresh_library_matches()
        for row in self.state.folders():
            # Folders you asked for (Get) are looked up even when auto-lookup is off.
            if not (self.settings.auto_preview or row.get("wanted_id")):
                continue
            w = self._wanted(row)
            if w and w["status"] not in ("arrived", "lookup", "review"):
                continue  # still downloading: the request looks it up when it arrives
            if row["status"] == "new" and now - row["changed_at"] >= self.settings.debounce:
                self.queue_preview(row["path"], auto=True, **self._wanted_search(row))

    def refresh_library_matches(self, only: str | None = None) -> None:
        """Re-check which inbox folders are already in the library (cheap SQL)."""
        for row in self.state.folders():
            if only and row["path"] != only:
                continue
            try:
                matches = library_matches(row["tagkeys"], row["tasks"])
            except Exception:  # noqa: BLE001 - never let this break a scan
                log.exception("library check failed for %s", row["path"])
                continue
            if matches != row["library"]:
                self.state.upsert_folder(row["path"], library=matches)

    def delete_imported(self) -> list[str]:
        """Delete every idle inbox folder that is imported or already in the library."""
        deleted = []
        for row in self.state.folders():
            if row["status"] in ("queued", "previewing", "importing"):
                continue
            if row["status"] == "imported" or row["library"]:
                try:
                    self.delete_source(row["path"])
                    deleted.append(row["path"])
                except (OSError, ValueError):
                    log.exception("could not delete %s", row["path"])
        return deleted

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

    def _wanted(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        wid = (row or {}).get("wanted_id")
        return self.state.wanted(wid) if wid else None

    def _wanted_search(self, row: dict[str, Any]) -> dict[str, Any]:
        """A requested folder is looked up by the release that was asked for."""
        w = self._wanted(row)
        if w is None:
            return {}
        ids = acquire.lookup_ids(w)
        return {"wanted": w["id"], **({"ids": ids} if ids else {})}

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
        ctx.note = err
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
        self.refresh_library_matches(only=rel)
        for t in tasks:
            ctx.log(
                f"  {t['cur_artist']} - {t['cur_album']}: {len(t['candidates'])} candidates"
                + (f", best {t['best']}%" if t["best"] is not None else "")
            )
        row = self.state.folder(rel) or {}
        wanted = self._wanted(row)
        # A folder you asked for imports on its own at its own threshold.
        threshold = self.settings.get_auto_import if wanted else self.settings.auto_import
        why = err or ""
        imported = False
        if ctx.params.get("auto") and status == "ready":
            library = [] if wanted and wanted["kind"] == "upgrade" else row.get("library") or []
            if not threshold:
                why = "automatic import is off"
            else:
                why = auto_import_blocker(tasks, library, threshold)
            if not why and wanted:
                why = _not_the_release(tasks, acquire.lookup_ids(wanted))
            if why:
                ctx.log(f"Not imported automatically: {why}")
            else:
                ctx.log(f"Every match is {threshold}% or better: importing automatically")
                decisions = _default_decisions(tasks)
                if wanted and wanted["kind"] == "upgrade":
                    # A better copy replaces the album it was asked for.
                    for d in decisions.values():
                        d["duplicate"] = "remove"
                self.queue_import(rel, decisions, user="auto-import")
                imported = True
        if wanted:
            acquire.inbox_previewed(self.state, wanted, imported, why)

    def _job_import(self, ctx: JobContext) -> None:
        rel = ctx.params["folder"]
        row = self.state.folder(rel)
        if row is None:
            raise KeyError(rel)
        path = self.resolve(rel)
        self.state.upsert_folder(rel, status="importing")
        try:
            outcomes = importer.apply_folder(
                str(path),
                row["decisions"],
                row["mode"],
                primary_artist=self.settings.primary_artist,
                aliases=self.state.artist_aliases(),
                romanize=self.settings.romanize,
                artist_romanizer=romanize.ArtistRomanizer(self.state),
            )
            if self.settings.romanize:
                romanize.copy_originals_to_albums()
        except Exception as e:
            self.state.upsert_folder(rel, status="failed", error=str(e))
            raise
        wanted = self._wanted(row)
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
            if wanted:
                acquire.inbox_imported(self.state, wanted, False, "already in your library")
            return
        if problems:
            self.state.upsert_folder(rel, status="attention", error="; ".join(problems))
            if wanted:
                acquire.inbox_imported(self.state, wanted, False, "; ".join(problems))
            return
        if not imported:
            self.state.upsert_folder(rel, status="ready", error="Nothing was imported")
            if wanted:
                acquire.inbox_imported(self.state, wanted, False, "nothing was imported")
            return
        if wanted:
            acquire.inbox_imported(self.state, wanted, True)
        if path.exists() and self.settings.delete_after_import:
            ctx.log(f"Deleting source folder {path}")
            shutil.rmtree(path, ignore_errors=True)
            self.state.delete_folder(rel)
            return
        sig, files = scan_folder(path) if path.exists() else ("", [])
        self.state.upsert_folder(
            rel, status="imported", signature=sig, files=files, error=None, duplicates={}
        )
        self.refresh_library_matches(only=rel)


def auto_import_blocker(tasks: list[dict], library: list[dict], threshold: float) -> str:
    """Why a looked-up folder must wait for review ("" if it can import on its own)."""
    if library:
        return "it looks like it is already in the library"
    for t in tasks:
        name = f"{t['cur_artist'] or '?'} - {t['cur_album'] or '?'}"
        if not t["candidates"]:
            return f"no candidates for {name}"
        c = t["candidates"][0]
        if c["match"] < threshold:
            return f"best match for {name} is {c['match']}% (needs {threshold}%)"
        if c.get("extra_tracks"):
            return f"{name} is missing {len(c['extra_tracks'])} track(s)"
        if c.get("extra_items"):
            return f"{name} has {len(c['extra_items'])} file(s) that match no track"
    return ""


def _not_the_release(tasks: list[dict], ids: list[str]) -> str:
    """Why a requested folder must wait: its best match is another release than the one asked for."""
    if not ids:
        return ""
    for t in tasks:
        best = t["candidates"][0] if t["candidates"] else None
        if best and str(best["id"]) not in ids:
            return f"it matches {best.get('album') or 'another album'}, not the album asked for"
    return ""


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

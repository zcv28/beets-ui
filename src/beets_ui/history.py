"""Before-state snapshots of library edits, and the job that restores them.

Every job that changes album or track fields records the old values of the
fields it touches first. Undo writes those values back with the same
write/move options the edit used, so tags and folders follow as well.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import beets_ctx
from .jobs import JobContext, JobQueue

# Job kinds that record a before-state and can be undone.
UNDOABLE = ("album-edit", "item-edit", "artists-primary", "artists-merge", "romanize")
SKIP = {"id", "path", "artpath", "album_id"}


def _jsonable(v: Any) -> Any:
    if isinstance(v, bytes):
        return os.fsdecode(v)
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


class Snapshot:
    """Collects old field values while a job runs; saved once at the end."""

    def __init__(self, ctx: JobContext, label: str):
        self.ctx = ctx
        self.label = label
        self.entries: list[dict[str, Any]] = []
        self._entries: dict[tuple[str, int], dict[str, Any]] = {}

    def capture(self, obj: Any, keys: Any) -> None:
        """Remember the current value of ``keys`` (the first capture of a key wins)."""
        kind = "album" if hasattr(obj, "item_dir") else "item"
        entry = self._entries.get((kind, obj.id))
        if entry is None:
            entry = {"type": kind, "id": obj.id, "fields": {}, "absent": []}
            self._entries[(kind, obj.id)] = entry
            self.entries.append(entry)
        present = set(obj.keys())  # noqa: SIM118 - beets models are not dicts
        for k in dict.fromkeys(keys):
            if k in SKIP or k in entry["fields"] or k in entry["absent"]:
                continue
            if k in present:
                entry["fields"][k] = _jsonable(obj.get(k))
            else:
                entry["absent"].append(k)

    def capture_album(self, album: Any, keys: Any) -> None:
        """An album and its tracks: album fields are copied down to the tracks."""
        keys = list(keys)
        self.capture(album, keys)
        item_keys = [k for k in keys if k in album.item_keys]
        if item_keys:
            for item in album.items():
                self.capture(item, item_keys)

    def save(self, write: bool, move: bool, **extra: Any) -> int | None:
        entries = [e for e in self.entries if e["fields"] or e["absent"]]
        if not entries:
            return None
        return self.ctx.state.add_snapshot(
            self.ctx.job_id,
            self.label,
            {"entries": entries, "write": write, "move": move, **extra},
        )


def _restore(lib: Any, entry: dict[str, Any], write: bool, move: bool, ctx: JobContext) -> bool:
    obj = lib.get_album(entry["id"]) if entry["type"] == "album" else lib.get_item(entry["id"])
    if obj is None:
        ctx.log(f"{entry['type']} {entry['id']} no longer exists, skipped")
        return False
    for k, v in entry["fields"].items():
        obj[k] = v
    for k in entry.get("absent", []):
        if k in obj and k not in obj._fields:
            del obj[k]
    obj.try_sync(write=write, move=move)
    return True


def register_jobs(jobs: JobQueue, image_names: list[str] | None = None) -> None:
    @jobs.register("undo")
    def undo(ctx: JobContext) -> None:
        from .artists import prune_dirs

        state = ctx.state
        snap = state.snapshot(int(ctx.params["snapshot_id"]))
        if snap is None:
            raise KeyError("snapshot not found")
        if snap["undone_by"]:
            raise RuntimeError(f"already undone by job #{snap['undone_by']}")
        lib = beets_ctx.lib()
        data = snap["data"]
        write, move = bool(data.get("write")), bool(data.get("move"))
        ctx.log(f"Undoing: {snap['label']}")
        old_dirs: set[Path] = set()
        if move:
            for e in data["entries"]:
                if e["type"] == "item" and (item := lib.get_item(e["id"])) is not None:
                    old_dirs.update({item.filepath.parent, item.filepath.parent.parent})
        n = 0
        # Albums first (their fields are copied down to the tracks), then the
        # tracks' own old values on top.
        for e in sorted(data["entries"], key=lambda e: e["type"] != "album"):
            n += _restore(lib, e, write, move, ctx)
        if move:
            root = Path(os.fsdecode(lib.directory))
            for d in prune_dirs(old_dirs, root, image_names or []):
                ctx.log(f"removed empty folder {d}")
        for alias in data.get("aliases", []):
            state.delete_artist_alias(alias)
            ctx.log(f"forgot the alias {alias!r}")
        state.mark_snapshot_undone(snap["id"], ctx.job_id)
        ctx.log(f"Restored {n} album/track record(s)" + (", wrote tags" if write else ""))

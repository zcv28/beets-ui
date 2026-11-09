"""Non-interactive beets import sessions.

Imports run in two phases so no session has to stay alive between requests:

* ``CaptureSession`` runs candidate lookup for a folder and records every
  task's candidates, then skips everything.
* ``ApplySession`` re-runs the folder with the user's per-task decisions
  (apply a specific candidate, as-is, skip) and duplicate resolution.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from beets.autotag import AlbumMatch, tag_album, tag_item
from beets.importer import Action, DuplicateAction, ImportSession

from . import artists, beets_ctx
from .candidates import _s, item_dict, task_dict, task_key

log = logging.getLogger("beets_ui.importer")

DUPLICATE_ACTIONS = {
    "skip": DuplicateAction.SKIP,
    "keep": DuplicateAction.KEEP,
    "remove": DuplicateAction.REMOVE,
    "merge": DuplicateAction.MERGE,
    "upgrade": DuplicateAction.UPGRADE,
}


def _album_summary(obj: Any) -> dict[str, Any]:
    """Describe an existing library album/item that conflicts with an import."""
    if hasattr(obj, "items") and callable(obj.items):
        items = list(obj.items())
        first = items[0] if items else None
        return {
            "id": obj.id,
            "type": "album",
            "albumartist": obj.albumartist,
            "album": obj.album,
            "year": obj.year,
            "tracks": len(items),
            "format": first.format if first else "",
            "bitrate": (int(first.bitrate or 0) // 1000) if first else 0,
            "path": _s(obj.item_dir()) if items else "",
        }
    return {
        "id": obj.id,
        "type": "item",
        "albumartist": obj.artist,
        "album": obj.title,
        "year": obj.year,
        "tracks": 1,
        "format": obj.format,
        "bitrate": int(obj.bitrate or 0) // 1000,
        "path": _s(obj.path),
    }


def _base_overrides(mode: str) -> dict[str, Any]:
    return {
        "import": {
            "resume": False,
            "incremental": False,
            "quiet": False,
            "pretend": False,
            "singletons": mode == "tracks",
            "group_albums": mode == "grouped",
            "autotag": True,
        }
    }


class _NoResume:
    def should_resume(self, path: bytes) -> bool:  # noqa: ARG002
        return False


class CaptureSession(_NoResume, ImportSession):
    """Collect candidates for every task in a folder, import nothing."""

    def __init__(
        self,
        lib: Any,
        paths: list[bytes],
        search_artist: str | None = None,
        search_name: str | None = None,
        search_ids: list[str] | None = None,
    ):
        super().__init__(lib, None, paths, None)
        self.search_artist = search_artist or None
        self.search_name = search_name or None
        self.search_ids = [i for i in (search_ids or []) if i]
        self.tasks: list[dict[str, Any]] = []

    def _relookup(self, task: Any) -> None:
        if not (self.search_artist or self.search_name):
            return
        fn = tag_album if task.is_album else tag_item
        task.candidates, task.rec = fn(
            task.source, search_artist=self.search_artist, search_name=self.search_name
        )

    def choose_match(self, task: Any) -> Action:
        self._relookup(task)
        self.tasks.append(task_dict(task))
        return Action.SKIP

    def choose_item(self, task: Any) -> Action:
        self._relookup(task)
        self.tasks.append(task_dict(task))
        return Action.SKIP


@dataclass
class TaskOutcome:
    key: str
    result: str  # imported | skipped | duplicate | missing-candidate
    detail: str = ""
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    current: dict[str, Any] = field(default_factory=dict)


class ApplySession(_NoResume, ImportSession):
    """Import a folder according to stored decisions.

    ``decisions`` maps task key -> {"action": "apply"|"asis"|"skip",
    "id": candidate id, "source": data source, "duplicate": action name}.
    """

    def __init__(
        self,
        lib: Any,
        paths: list[bytes],
        decisions: dict[str, dict[str, Any]],
        primary_artist: bool = False,
    ):
        super().__init__(lib, None, paths, None)
        self.decisions = decisions
        self.primary_artist = primary_artist
        self.outcomes: dict[str, TaskOutcome] = {}
        self._merge_decision: dict[str, Any] | None = None

    def _decision(self, task: Any) -> tuple[str, dict[str, Any] | None]:
        key = task_key(task.paths)
        dec = self.decisions.get(key)
        if dec is None and task.toppath is None and self._merge_decision is not None:
            # A merge task created by beets after "merge" duplicate handling.
            return key, self._merge_decision
        return key, dec

    def _choose(self, task: Any) -> AlbumMatch | Action | Any:
        key, dec = self._decision(task)
        if not dec or dec.get("action") == "skip":
            self.outcomes[key] = TaskOutcome(key, "skipped", "no decision" if not dec else "")
            return Action.SKIP
        if dec["action"] == "asis":
            self.outcomes[key] = TaskOutcome(key, "imported", "as-is")
            return Action.ASIS
        wanted_id, wanted_src = dec.get("id"), dec.get("source")
        for cand in task.candidates or []:
            info = cand.info
            cid = info.album_id if task.is_album else info.track_id
            if cid == wanted_id and (not wanted_src or info.data_source == wanted_src):
                detail = f"{info.data_source} {wanted_id}"
                if self.primary_artist and task.is_album:
                    credit = info.artist
                    if primary := artists.apply_to_info(info, artists.known_artists(self.lib)):
                        detail += f", album artist {credit!r} -> {primary!r}"
                self.outcomes[key] = TaskOutcome(key, "imported", detail)
                return cand
        self.outcomes[key] = TaskOutcome(
            key, "missing-candidate", f"candidate {wanted_src} {wanted_id} not returned by lookup"
        )
        return Action.SKIP

    def choose_match(self, task: Any) -> Any:
        return self._choose(task)

    def choose_item(self, task: Any) -> Any:
        return self._choose(task)

    def get_duplicate_action(self, task: Any, found_duplicates: list[Any]) -> DuplicateAction:
        key, dec = self._decision(task)
        choice = (dec or {}).get("duplicate")
        if choice in DUPLICATE_ACTIONS:
            action = DUPLICATE_ACTIONS[choice]
            if action is DuplicateAction.MERGE:
                self._merge_decision = dec
            if action is DuplicateAction.SKIP:
                self.outcomes[key] = TaskOutcome(key, "skipped", "duplicate skipped")
            return action
        self.outcomes[key] = TaskOutcome(
            key,
            "duplicate",
            "already in library",
            duplicates=[_album_summary(d) for d in found_duplicates],
            current={"items": [item_dict(i) for i in task.items]},
        )
        return DuplicateAction.SKIP


# ----------------------------------------------------------------- helpers


def _paths(folder: str) -> list[bytes]:
    return [os.fsencode(folder)]


def preview_folder(
    folder: str,
    mode: str = "albums",
    search_artist: str | None = None,
    search_name: str | None = None,
    search_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    lib = beets_ctx.lib()
    session = CaptureSession(lib, _paths(folder), search_artist, search_name, search_ids)
    overrides = _base_overrides(mode)
    if search_ids:
        overrides["import"]["search_ids"] = list(search_ids)
    with beets_ctx.config_overrides(overrides):
        session.run()
    return session.tasks


def apply_folder(
    folder: str,
    decisions: dict[str, dict[str, Any]],
    mode: str = "albums",
    primary_artist: bool = False,
) -> dict[str, TaskOutcome]:
    lib = beets_ctx.lib()
    session = ApplySession(lib, _paths(folder), decisions, primary_artist)
    ids = sorted(
        {d["id"] for d in decisions.values() if d.get("action") == "apply" and d.get("id")}
    )
    overrides = _base_overrides(mode)
    # Restrict lookup to the chosen IDs. With only as-is/skip choices the
    # lookup still runs but its result is ignored.
    overrides["import"]["search_ids"] = ids
    with beets_ctx.config_overrides(overrides):
        session.run()
    return session.outcomes

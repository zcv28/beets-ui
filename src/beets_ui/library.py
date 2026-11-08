"""Library browsing helpers and write jobs (edit, remove)."""

from __future__ import annotations

import os
import shlex
from typing import Any

from beets.dbcore.query import ParsingError

from . import beets_ctx
from .jobs import JobContext, JobQueue

ALBUM_FIELDS = [
    "albumartist",
    "album",
    "year",
    "original_year",
    "genres",
    "label",
    "catalognum",
    "albumtype",
    "country",
    "comp",
    "albumdisambig",
    "mb_albumid",
    "mb_releasegroupid",
]
ITEM_FIELDS = [
    "title",
    "artist",
    "track",
    "disc",
    "year",
    "genres",
    "composers",
    "comments",
    "lyrics",
    "mb_trackid",
]

ALBUM_SORTS = {
    "artist": "albumartist+ year+ album+",
    "album": "album+",
    "year": "year- albumartist+",
    "added": "added-",
}


class QueryError(ValueError):
    pass


def _parts(query: str) -> list[str]:
    try:
        return shlex.split(query or "")
    except ValueError as e:
        raise QueryError(str(e)) from e


def albums(query: str = "", sort: str = "artist", page: int = 1, per_page: int = 60):
    parts = _parts(query) + ALBUM_SORTS.get(sort, ALBUM_SORTS["artist"]).split()
    lib = beets_ctx.lib()
    try:
        results = lib.albums(parts)
        start = (page - 1) * per_page
        rows = list(results[start : start + per_page + 1])
    except (ParsingError, ValueError) as e:
        raise QueryError(str(e)) from e
    return rows[:per_page], len(rows) > per_page


def items(query: str = "", page: int = 1, per_page: int = 100, singletons: bool = False):
    parts = _parts(query) + (["singleton:true"] if singletons else []) + ["artist+", "title+"]
    lib = beets_ctx.lib()
    try:
        results = lib.items(parts)
        start = (page - 1) * per_page
        rows = list(results[start : start + per_page + 1])
    except (ParsingError, ValueError) as e:
        raise QueryError(str(e)) from e
    return rows[:per_page], len(rows) > per_page


def stats() -> dict[str, Any]:
    lib = beets_ctx.lib()
    with lib.transaction() as tx:
        n_items, total_len, total_size = tx.query(
            "SELECT COUNT(*), COALESCE(SUM(length),0), 0 FROM items"
        )[0]
        n_albums = tx.query("SELECT COUNT(*) FROM albums")[0][0]
        n_artists = tx.query("SELECT COUNT(DISTINCT albumartist) FROM albums")[0][0]
    return {"items": n_items, "albums": n_albums, "artists": n_artists, "hours": total_len / 3600}


def parse_extra_fields(text: str) -> dict[str, str]:
    """Parse ``field=value`` lines; ``field!`` deletes a flexible field."""
    out: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("!") and "=" not in line:
            out[line[:-1].strip()] = "\0delete"
            continue
        if "=" not in line:
            raise ValueError(f"expected field=value, got {line!r}")
        k, v = line.split("=", 1)
        k = k.strip()
        if not k.replace("_", "").isalnum():
            raise ValueError(f"invalid field name {k!r}")
        out[k] = v.strip()
    return out


def _apply(obj: Any, fields: dict[str, str], ctx: JobContext) -> bool:
    changed = False
    for key, value in fields.items():
        if key in ("id", "path", "artpath", "album_id"):
            raise ValueError(f"field {key} cannot be edited")
        if value == "\0delete":
            if key in obj and key not in obj._fields:
                del obj[key]
                ctx.log(f"  {key}: deleted")
                changed = True
            continue
        old = obj.get(key)
        obj.set_parse(key, value)
        if obj.get(key) != old:
            ctx.log(f"  {key}: {old!r} -> {obj.get(key)!r}")
            changed = True
    return changed


def register_jobs(jobs: JobQueue) -> None:
    @jobs.register("album-edit")
    def album_edit(ctx: JobContext) -> None:
        album = beets_ctx.lib().get_album(int(ctx.params["album_id"]))
        if album is None:
            raise KeyError("album not found")
        ctx.log(f"Editing album {album.albumartist} - {album.album}")
        if _apply(album, ctx.params["fields"], ctx) or ctx.params.get("move"):
            album.try_sync(write=bool(ctx.params.get("write")), move=bool(ctx.params.get("move")))
            ctx.log("Saved" + (" and wrote tags" if ctx.params.get("write") else ""))
        else:
            ctx.log("Nothing changed")

    @jobs.register("item-edit")
    def item_edit(ctx: JobContext) -> None:
        item = beets_ctx.lib().get_item(int(ctx.params["item_id"]))
        if item is None:
            raise KeyError("track not found")
        ctx.log(f"Editing track {item.artist} - {item.title}")
        if _apply(item, ctx.params["fields"], ctx) or ctx.params.get("move"):
            item.try_sync(write=bool(ctx.params.get("write")), move=bool(ctx.params.get("move")))
            ctx.log("Saved")
        else:
            ctx.log("Nothing changed")

    @jobs.register("album-remove")
    def album_remove(ctx: JobContext) -> None:
        album = beets_ctx.lib().get_album(int(ctx.params["album_id"]))
        if album is None:
            raise KeyError("album not found")
        delete = bool(ctx.params.get("delete"))
        ctx.log(
            f"Removing {album.albumartist} - {album.album}"
            + (" and deleting files" if delete else "")
        )
        album.remove(delete=delete, with_items=True)

    @jobs.register("item-remove")
    def item_remove(ctx: JobContext) -> None:
        item = beets_ctx.lib().get_item(int(ctx.params["item_id"]))
        if item is None:
            raise KeyError("track not found")
        delete = bool(ctx.params.get("delete"))
        ctx.log(f"Removing {item.artist} - {item.title}" + (" and deleting file" if delete else ""))
        item.remove(delete=delete, with_album=True)


def album_dict(album: Any) -> dict[str, Any]:
    return {
        "id": album.id,
        "albumartist": album.albumartist,
        "album": album.album,
        "year": album.year,
        "genres": album.get("genres"),
        "label": album.label,
        "has_art": bool(album.artpath and os.path.isfile(album.artpath)),
    }

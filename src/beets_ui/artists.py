"""Keep only the primary artist as album artist ("A & B" -> "A").

Sources such as MusicBrainz credit collaborations as one joined album artist
("ヌジャベス & Joe Claussell"), which gives every collaboration its own artist
entry and folder. The track artist keeps the full credit; only the album-level
artist fields are reduced to the first credited artist.

The primary artist is taken from the multi-valued ``albumartists`` field when
the source filled it. Otherwise the name is split on common separators, and the
first part is used only if it is already an album artist in the library, so
duo names such as "Simon & Garfunkel" are left alone.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import beets_ctx
from .jobs import JobContext, JobQueue

SPLIT = re.compile(
    r"\s+(?:&|and|feat\.?|ft\.?|featuring|with|vs\.?|x)\s+"
    r"|\s*(?:[,、，×＆]|\s/\s|／)\s*",
    re.IGNORECASE,
)

# AlbumInfo scalar attribute -> matching list attribute
INFO_FIELDS = {
    "artist": "artists",
    "artist_sort": "artists_sort",
    "artist_credit": "artists_credit",
    "artist_id": "artists_ids",
}
CLUTTER = {"thumbs.db", ".ds_store", "desktop.ini"}


def known_artists(lib: Any) -> dict[str, dict[str, str]]:
    """Album artists in the library: casefolded name -> reference fields.

    Albums credited to a single artist win over joined credits, so the sort
    name and MusicBrainz ID of the artist's own albums are reused.
    """
    with lib.transaction() as tx:
        rows = tx.query(
            "SELECT albumartist, albumartist_sort, albumartist_credit, mb_albumartistid "
            "FROM albums WHERE albumartist != ''"
        )
    out: dict[str, dict[str, str]] = {}
    for name, sort, credit, mbid in rows:
        ref = {
            "name": name,
            "sort": sort or "",
            "credit": credit or "",
            "mbid": (mbid or "").split(";")[0].strip(),
        }
        key = name.casefold()
        if key not in out or (ref["mbid"] and not out[key]["mbid"]):
            out[key] = ref
    return out


def primary_of(name: str, artists: list[str] | None, known: dict[str, dict[str, str]]) -> str:
    """Return the primary artist for a joined credit, or "" to keep ``name``."""
    name = (name or "").strip()
    if not name:
        return ""
    artists = [a for a in (artists or []) if a]
    if len(artists) > 1 and artists[0] != name:
        return artists[0]
    parts = SPLIT.split(name, maxsplit=1)
    if len(parts) == 2 and parts[0].strip():
        ref = known.get(parts[0].strip().casefold())
        if ref and ref["name"] != name:
            return ref["name"]
    return ""


def _first(values: Any) -> str:
    values = list(values or [])
    return str(values[0]) if values else ""


def _primary_values(
    primary: str, lists: dict[str, list[str]], known: dict[str, dict[str, str]]
) -> dict[str, str]:
    """Sort name, credit and MBID for the primary artist."""
    use_lists = _first(lists["name"]) == primary
    vals = {
        "sort": _first(lists["sort"]) if use_lists else "",
        "credit": _first(lists["credit"]) if use_lists else "",
        "mbid": _first(lists["mbid"]) if use_lists else "",
    }
    ref = known.get(primary.casefold())
    if ref:
        for k in vals:
            vals[k] = ref[k] or vals[k]
    vals["credit"] = vals["credit"] or primary
    return vals


def album_changes(album: Any, known: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Field changes that reduce a library album to its primary artist."""
    primary = primary_of(album.albumartist, album.get("albumartists"), known)
    if not primary:
        return {}
    v = _primary_values(
        primary,
        {
            "name": album.get("albumartists"),
            "sort": album.get("albumartists_sort"),
            "credit": album.get("albumartists_credit"),
            "mbid": album.get("mb_albumartistids"),
        },
        known,
    )
    wanted = {
        "albumartist": primary,
        "albumartists": [primary],
        "albumartist_sort": v["sort"],
        "albumartists_sort": [v["sort"]] if v["sort"] else [],
        "albumartist_credit": v["credit"],
        "albumartists_credit": [v["credit"]],
        "mb_albumartistid": v["mbid"],
        "mb_albumartistids": [v["mbid"]] if v["mbid"] else [],
    }
    return {k: val for k, val in wanted.items() if album.get(k) != val}


def apply_to_info(info: Any, known: dict[str, dict[str, str]]) -> str:
    """Reduce a candidate AlbumInfo to its primary artist before it is applied.

    Returns the primary artist, or "" when nothing changed. Tracks without
    their own artist keep the full credit instead of inheriting the primary.
    """
    original = info.artist or ""
    primary = primary_of(original, info.get("artists"), known)
    if not primary:
        return ""
    for t in info.tracks or []:
        if not t.get("artist"):
            for attr, list_attr in INFO_FIELDS.items():
                if info.get(attr) and not t.get(attr):
                    t[attr] = info[attr]
                if info.get(list_attr) and not t.get(list_attr):
                    t[list_attr] = list(info[list_attr])
    v = _primary_values(
        primary,
        {
            "name": info.get("artists"),
            "sort": info.get("artists_sort"),
            "credit": info.get("artists_credit"),
            "mbid": info.get("artists_ids"),
        },
        known,
    )
    info["artist"] = primary
    info["artists"] = [primary]
    info["artist_sort"] = v["sort"] or None
    info["artists_sort"] = [v["sort"]] if v["sort"] else []
    info["artist_credit"] = v["credit"]
    info["artists_credit"] = [v["credit"]]
    info["artist_id"] = v["mbid"] or None
    info["artists_ids"] = [v["mbid"]] if v["mbid"] else []
    # raw_data / item_data are cached properties: drop stale copies.
    for obj in (info, *(info.tracks or [])):
        vars(obj).pop("raw_data", None)
        vars(obj).pop("item_data", None)
    return primary


def proposals(lib: Any | None = None) -> list[dict[str, Any]]:
    """Every library album whose album artist would be reduced."""
    lib = lib or beets_ctx.lib()
    known = known_artists(lib)
    out = []
    for album in lib.albums():
        changes = album_changes(album, known)
        if changes:
            out.append(
                {
                    "id": album.id,
                    "album": album.album,
                    "year": album.year,
                    "current": album.albumartist,
                    "primary": changes.get("albumartist", album.albumartist),
                    "merges": changes.get("albumartist", "").casefold() in known,
                }
            )
    out.sort(key=lambda r: (r["primary"].casefold(), r["current"].casefold(), r["year"] or 0))
    return out


def _prune(dirs: set[Path], root: Path, image_names: list[str]) -> list[Path]:
    """Remove folders left with only artist pictures or clutter after a move."""
    removable = CLUTTER | {n.casefold() for n in image_names}
    removed = []
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        if d == root or root not in d.parents or not d.is_dir():
            continue
        entries = list(d.iterdir())
        if any(e.is_dir() or e.name.casefold() not in removable for e in entries):
            continue
        for e in entries:
            e.unlink()
        d.rmdir()
        removed.append(d)
    return removed


def register_jobs(jobs: JobQueue, image_names: list[str] | None = None) -> None:
    @jobs.register("artists-primary")
    def artists_primary(ctx: JobContext) -> None:
        lib = beets_ctx.lib()
        known = known_artists(lib)
        write, move = bool(ctx.params.get("write")), bool(ctx.params.get("move"))
        root = Path(os.fsdecode(lib.directory))
        old_dirs: set[Path] = set()
        done = 0
        for album_id in ctx.params.get("album_ids") or []:
            album = lib.get_album(int(album_id))
            if album is None:
                ctx.log(f"album {album_id} no longer exists, skipped")
                continue
            changes = album_changes(album, known)
            if not changes:
                ctx.log(f"{album.albumartist} - {album.album}: nothing to change")
                continue
            ctx.log(
                f"{album.albumartist} - {album.album} -> "
                f"{changes.get('albumartist', album.albumartist)}"
            )
            for item in album.items():
                old_dirs.update({item.filepath.parent, item.filepath.parent.parent})
            album.update(changes)
            album.try_sync(write=write, move=move)
            done += 1
        if move:
            for d in _prune(old_dirs, root, image_names or []):
                ctx.log(f"removed empty folder {d}")
        ctx.log(
            f"Updated {done} album{'s' if done != 1 else ''}"
            + (", wrote tags" if write else "")
            + (", moved files" if move else "")
        )

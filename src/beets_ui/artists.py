"""Keep only the primary artist as album artist ("A & B" -> "A").

Sources such as MusicBrainz credit collaborations as one joined album artist
("ヌジャベス & Joe Claussell"), which gives every collaboration its own artist
entry and folder. The track artist keeps the full credit; only the album-level
artist fields are reduced to the first credited artist.

The primary artist is taken from the multi-valued ``albumartists`` field when
the source filled it. Otherwise the name is split on common separators, and the
first part is used only if it is already an album artist in the library, so
duo names such as "Simon & Garfunkel" are left alone.

Aliases ("ヌジャベス" and "Nujabes") are merged into one album artist. The
merge is remembered, so later imports use the merged name too.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import beets_ctx
from .history import Snapshot
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


def known_artists(lib: Any, aliases: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
    """Album artists in the library: casefolded name -> reference fields.

    Albums credited to a single artist win over joined credits, so the sort
    name and MusicBrainz ID of the artist's own albums are reused. Remembered
    aliases resolve to the artist they were merged into.
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
    for alias, name in (aliases or {}).items():
        out[alias] = out.get(name.casefold()) or {
            "name": name,
            "sort": "",
            "credit": "",
            "mbid": "",
        }
    return out


def primary_of(name: str, artists: list[str] | None, known: dict[str, dict[str, str]]) -> str:
    """Return the primary artist for a joined credit, or "" to keep ``name``."""
    name = (name or "").strip()
    if not name:
        return ""
    artists = [a for a in (artists or []) if a]
    if len(artists) > 1 and artists[0] != name:
        ref = known.get(artists[0].casefold())
        return ref["name"] if ref else artists[0]
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


def proposals(lib: Any | None = None, aliases: dict[str, str] | None = None) -> list[dict]:
    """Every library album whose album artist would be reduced."""
    lib = lib or beets_ctx.lib()
    known = known_artists(lib, aliases)
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


# ------------------------------------------------------------------ aliases

VARIOUS_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"
PAREN = re.compile(r"^(.+?)\s*[(（\[［](.+?)[)）\]］]$")


def norm(name: str) -> str:
    """Comparison key: full-width/half-width, case and spacing differences ignored."""
    return " ".join(unicodedata.normalize("NFKC", name or "").casefold().split())


def apply_aliases_to_info(info: Any, aliases: dict[str, str], known: dict) -> str:
    """Rename remembered aliases in a candidate before it is applied.

    Returns the new album artist, or "" when the album artist is unchanged.
    Track artists are renamed only when the whole credit is an alias.
    """
    if not aliases:
        return ""

    def target(name: Any) -> str:
        return aliases.get(str(name or "").casefold(), "")

    def sort_of(name: str) -> str:
        return (known.get(name.casefold()) or {}).get("sort", "")

    for t in info.tracks or []:
        if new := target(t.get("artist")):
            t["artist"] = new
            t["artists"] = [new]
            t["artist_credit"] = new
            t["artists_credit"] = [new]
            t["artist_sort"] = sort_of(new) or None
            t["artists_sort"] = [sort_of(new)] if sort_of(new) else []
    # A romanized album still follows aliases saved under its original name.
    new = target(info.artist) or target(info.get("albumartist_original"))
    if new and new != info.artist:
        info["artist"] = new
        info["artists"] = [new]
        info["artist_credit"] = new
        info["artists_credit"] = [new]
        info["artist_sort"] = sort_of(new) or None
        info["artists_sort"] = [sort_of(new)] if sort_of(new) else []
    for obj in (info, *(info.tracks or [])):
        vars(obj).pop("raw_data", None)
        vars(obj).pop("item_data", None)
    return new


def alias_suggestions(lib: Any | None = None) -> list[dict[str, Any]]:
    """Groups of album artists that look like the same artist under several names.

    Two names are grouped when they differ only in width/case/spacing, share
    an artist ID from the same source, when one is the other's sort name or
    credited name, or when one is written "A (B)" and A or B is the other.
    """
    lib = lib or beets_ctx.lib()
    with lib.transaction() as tx:
        rows = tx.query(
            "SELECT a.albumartist, a.albumartist_sort, a.albumartist_credit, "
            "a.mb_albumartistid, COALESCE(f.value, '') FROM albums a "
            "LEFT JOIN album_attributes f ON f.entity_id = a.id AND f.key = 'data_source' "
            "WHERE a.albumartist != ''"
        )
    info: dict[str, dict[str, Any]] = {}
    for name, sort, credit, mbid, source in rows:
        a = info.setdefault(name, {"albums": 0, "other": set(), "ids": set()})
        a["albums"] += 1
        a["other"].update(n for n in (sort, credit) if n and n != name)
        mbid = (mbid or "").strip()
        if mbid and ";" not in mbid and mbid != VARIOUS_MBID and not SPLIT.search(name):
            a["ids"].add((source.casefold(), mbid))

    parent = {n: n for n in info}

    def find(n: str) -> str:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    reasons: dict[frozenset, str] = {}

    def union(a: str, b: str, why: str) -> None:
        if a == b:
            return
        reasons.setdefault(frozenset((a, b)), why)
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_norm: dict[str, str] = {}
    by_id: dict[tuple[str, str], str] = {}
    for name, a in info.items():
        key = norm(name)
        if key in by_norm:
            union(by_norm[key], name, "same name, different spelling")
        else:
            by_norm[key] = name
        for i in a["ids"]:
            if i in by_id:
                union(by_id[i], name, f"same {i[0] or 'artist'} ID")
            else:
                by_id[i] = name
    for name, a in info.items():
        for other in a["other"]:
            if (match := by_norm.get(norm(other))) and match != name:
                union(match, name, f"{match!r} is the sort or credited name of {name!r}")
        if m := PAREN.match(name):
            for part in m.groups():
                if (match := by_norm.get(norm(part))) and match != name:
                    union(match, name, f"{name!r} contains {match!r}")

    groups: dict[str, list[str]] = defaultdict(list)
    for name in info:
        groups[find(name)].append(name)
    out = []
    for names in groups.values():
        if len(names) < 2:
            continue
        names.sort(
            key=lambda n: (
                -info[n]["albums"],
                -len(info[n]["ids"]),
                bool(PAREN.match(n)),
                n.casefold(),
            )
        )
        out.append(
            {
                "names": [{"name": n, "albums": info[n]["albums"]} for n in names],
                "target": names[0],
                "why": sorted({w for pair, w in reasons.items() if pair <= set(names)}),
            }
        )
    out.sort(key=lambda g: g["target"].casefold())
    return out


def prune_dirs(dirs: set[Path], root: Path, image_names: list[str]) -> list[Path]:
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
        known = known_artists(lib, ctx.state.artist_aliases())
        write, move = bool(ctx.params.get("write")), bool(ctx.params.get("move"))
        root = Path(os.fsdecode(lib.directory))
        old_dirs: set[Path] = set()
        snap = Snapshot(ctx, "Keep primary album artist")
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
            snap.capture_album(album, changes)
            album.update(changes)
            album.try_sync(write=write, move=move)
            done += 1
        snap.save(write, move)
        if move:
            for d in prune_dirs(old_dirs, root, image_names or []):
                ctx.log(f"removed empty folder {d}")
        ctx.log(
            f"Updated {done} album{'s' if done != 1 else ''}"
            + (", wrote tags" if write else "")
            + (", moved files" if move else "")
        )

    @jobs.register("artists-merge")
    def artists_merge(ctx: JobContext) -> None:
        from .romanize import is_latin

        lib = beets_ctx.lib()
        target = ctx.params["target"].strip()
        keys = {a.casefold() for a in ctx.params["aliases"] if a.strip()} - {target.casefold()}
        if not target or not keys:
            raise ValueError("nothing to merge")
        write, move = bool(ctx.params.get("write")), bool(ctx.params.get("move"))
        sort = (known_artists(lib).get(target.casefold()) or {}).get("sort", "")
        root = Path(os.fsdecode(lib.directory))
        snap = Snapshot(ctx, f"Merge {', '.join(sorted(ctx.params['aliases']))} into {target}")
        old_dirs: set[Path] = set()
        n_albums = n_items = 0
        for album in list(lib.albums()):
            if album.albumartist.casefold() not in keys:
                continue
            changes = {
                "albumartist": target,
                "albumartists": [target],
                "albumartist_sort": sort,
                "albumartists_sort": [sort] if sort else [],
                "albumartist_credit": target,
                "albumartists_credit": [target],
            }
            if not is_latin(album.albumartist) and is_latin(target):
                changes.setdefault("albumartist_original", album.albumartist)
            changes = {k: v for k, v in changes.items() if album.get(k) != v}
            ctx.log(f"{album.albumartist} - {album.album} -> {target}")
            for item in album.items():
                old_dirs.update({item.filepath.parent, item.filepath.parent.parent})
            snap.capture_album(album, changes)
            album.update(changes)
            album.try_sync(write=write, move=move)
            n_albums += 1
        if ctx.params.get("tracks", True):
            for item in list(lib.items()):
                if (item.artist or "").casefold() not in keys:
                    continue
                changes = {
                    "artist": target,
                    "artists": [target],
                    "artist_sort": sort,
                    "artists_sort": [sort] if sort else [],
                    "artist_credit": target,
                    "artists_credit": [target],
                }
                if not is_latin(item.artist) and is_latin(target):
                    changes.setdefault("artist_original", item.artist)
                changes = {k: v for k, v in changes.items() if item.get(k) != v}
                if not item.album_id:
                    old_dirs.update({item.filepath.parent, item.filepath.parent.parent})
                snap.capture(item, changes)
                item.update(changes)
                item.try_sync(write=write, move=move)
                n_items += 1
        aliases = sorted(ctx.params["aliases"])
        for alias in aliases:
            if alias.casefold() != target.casefold():
                ctx.state.set_artist_alias(alias, target)
        snap.save(write, move, aliases=[a for a in aliases if a.casefold() != target.casefold()])
        if move:
            for d in prune_dirs(old_dirs, root, image_names or []):
                ctx.log(f"removed empty folder {d}")
        ctx.log(
            f"Merged into {target}: {n_albums} album(s), {n_items} track artist(s)"
            + (", wrote tags" if write else "")
            + (", moved files" if move else "")
            + ". Future imports use this name too."
        )

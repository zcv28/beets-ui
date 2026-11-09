"""Romanized names as the main tags, with the original names kept alongside.

Jellyfin and most players only show the main tags, so these get the
romanized album, artist and track names. The names as originally written
are stored in ``album_original``, ``albumartist_original``,
``title_original`` and ``artist_original`` (in the library and as custom
tags in the files), and beets-ui shows and searches both.

Romanized names come from the source the album was tagged from:

* VocaDB, UtaiteDB, TouhouDB: the same release asked for in Romaji and in
  Japanese (the beets-vocadb plugin builds both, so artist strings match
  what it normally produces).
* MusicBrainz: the release's Latin-script pseudo-release
  ("transliterated tracklisting"), when someone has added one.

When the source has no romanized names nothing changes.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

import httpx

from . import beets_ctx
from .history import Snapshot
from .jobs import JobContext, JobQueue

log = logging.getLogger("beets_ui.romanize")

ORIGINAL_ALBUM_FIELDS = ("album_original", "albumartist_original")
ORIGINAL_TRACK_FIELDS = ("title_original", "artist_original")
ORIGINAL_FIELDS = ORIGINAL_ALBUM_FIELDS + ORIGINAL_TRACK_FIELDS
VOCADB_SOURCES = {"vocadb", "utaitedb", "touhoudb"}
SUPPORTED = VOCADB_SOURCES | {"musicbrainz"}
LATIN = "Latn"


class RomanizeError(RuntimeError):
    pass


def register_media_fields() -> None:
    """Write the ``*_original`` fields to the files as custom tags."""
    import mediafile
    from beets import library

    for name in ORIGINAL_FIELDS:
        if name not in mediafile.MediaFile.__dict__:
            tag = name.upper()
            mediafile.MediaFile.add_field(
                name,
                mediafile.MediaField(
                    mediafile.MP3DescStorageStyle(tag),
                    mediafile.MP4StorageStyle(f"----:com.apple.iTunes:{tag}"),
                    mediafile.StorageStyle(tag),
                    mediafile.ASFStorageStyle(tag),
                ),
            )
        library.Item._media_fields.add(name)


# --------------------------------------------------------------- lookups


def _plugin(source: str) -> Any:
    from beets import metadata_plugins

    plugin = metadata_plugins.get_metadata_source(source)
    if plugin is None:
        raise RomanizeError(f"the {source} plugin is not enabled")
    return plugin


def _vocadb(plugin: Any, album_id: str, lang: str) -> Any:
    """One VocaDB-style album in the given language, built by the plugin itself."""
    try:
        url = (
            f"{plugin.instance_info.api_url}albums/{album_id}"
            f"?fields=Artists,Discs,Tags,Tracks,WebLinks&songFields={plugin.song_fields}"
            f"&lang={lang}"
        )
        headers = plugin.headers
    except AttributeError as e:  # a beets-vocadb version we don't know
        raise RomanizeError(f"unsupported {plugin.data_source} plugin: {e}") from e
    r = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return plugin.album_info(r.json(), search_lang=lang)


def _script(release: dict) -> str:
    return (release.get("text_representation") or {}).get("script") or ""


def _musicbrainz(plugin: Any, album_id: str) -> tuple[Any, Any] | None:
    raw = plugin.mb_api.get_release(album_id)
    rels = [r for r in raw.get("release_relations", []) if r.get("type") == "transl-tracklisting"]
    if raw.get("status") == "Pseudo-Release":
        if _script(raw) != LATIN:
            return None
        official = next((r for r in rels if r.get("direction") == "backward"), None)
        if official is None:
            return None
        return (
            plugin.album_info(raw),
            plugin.album_info(plugin.mb_api.get_release(official["release"]["id"])),
        )
    if _script(raw) == LATIN:
        return None  # already written in Latin script
    pseudo = next(
        (
            r
            for r in rels
            if r.get("direction") == "forward" and _script(r.get("release") or {}) == LATIN
        ),
        None,
    )
    if pseudo is None:
        return None
    return plugin.album_info(plugin.mb_api.get_release(pseudo["release"]["id"])), plugin.album_info(
        raw
    )


def fetch_pair(source: str, album_id: str) -> tuple[Any, Any] | None:
    """(romanized, original) AlbumInfo of one release, or None without romanized names."""
    src = (source or "").lower()
    if not album_id or src not in SUPPORTED:
        return None
    plugin = _plugin(source)
    if src in VOCADB_SOURCES:
        roman = _vocadb(plugin, album_id, "Romaji")
        original = _vocadb(plugin, album_id, "Japanese")
        return roman, original
    return _musicbrainz(plugin, album_id)


# ---------------------------------------------------------------- names


def _track_names(info: Any) -> tuple[dict[str, Any], list[Any]]:
    tracks = list(info.tracks or [])
    return {str(t.track_id): t for t in tracks if t.get("track_id")}, tracks


def _match(track: Any, index: int, by_id: dict[str, Any], ordered: list[Any]) -> Any:
    found = by_id.get(str(track.get("track_id") or ""))
    if found is None and index < len(ordered):
        found = ordered[index]
    return found


def _artist_fields(prefix: str, name: str) -> dict[str, Any]:
    """Main artist fields for a romanized name (``artist`` or ``albumartist``)."""
    many = "artists" if prefix == "artist" else "albumartists"
    return {
        prefix: name,
        many: [name],
        f"{prefix}_credit": name,
        f"{many}_credit": [name],
    }


def romanize_info(info: Any, pair: tuple[Any, Any] | None = None) -> bool:
    """Rename a candidate to romanized names before it is applied.

    The original names go into ``*_original`` keys, which beets copies onto
    every track. Returns True when anything changed.
    """
    pair = pair if pair is not None else fetch_pair(info.data_source, info.album_id)
    if pair is None:
        return False
    roman, original = pair
    changed = False
    album_extra: dict[str, Any] = {}
    if roman.album and roman.album != original.album:
        info["album"] = roman.album
        album_extra["album_original"] = original.album
        changed = True
    if roman.artist and roman.artist != original.artist:
        info.update(_artist_fields("artist", roman.artist))  # AlbumInfo.artist is the album artist
        album_extra["albumartist_original"] = original.artist
        changed = True
    info.update(album_extra)
    r_ids, r_list = _track_names(roman)
    o_ids, o_list = _track_names(original)
    for i, t in enumerate(info.tracks or []):
        r, o = _match(t, i, r_ids, r_list), _match(t, i, o_ids, o_list)
        if r is None or o is None:
            continue
        if r.title and r.title != o.title:
            t["title"] = r.title
            t["title_original"] = o.title
            changed = True
        if r.artist and r.artist != o.artist:
            t.update(_artist_fields("artist", r.artist))
            t["artist_original"] = o.artist
            changed = True
    for obj in (info, *(info.tracks or [])):
        vars(obj).pop("raw_data", None)
        vars(obj).pop("item_data", None)
    return changed


def copy_originals_to_albums(lib: Any | None = None) -> int:
    """Album-level original names live on the tracks after an import: copy them up."""
    lib = lib or beets_ctx.lib()
    with lib.transaction() as tx:
        rows = tx.query(
            "SELECT DISTINCT i.album_id FROM items i JOIN item_attributes f "
            "ON f.entity_id = i.id AND f.key IN ('album_original', 'albumartist_original') "
            "WHERE i.album_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM album_attributes g "
            "WHERE g.entity_id = i.album_id AND g.key = f.key)"
        )
    n = 0
    for (album_id,) in rows:
        album = lib.get_album(album_id)
        items = list(album.items()) if album else []
        if not items:
            continue
        for key in ORIGINAL_ALBUM_FIELDS:
            if items[0].get(key) and key not in album:
                album[key] = items[0][key]
        album.store(inherit=False)
        n += 1
    return n


def originals_query(
    terms: list[str], fields: tuple[str, ...], always: list[str] | None = None
) -> list[str]:
    """Free-text query parts that also match the original names.

    ``["foo"]`` becomes ``foo , album_original:foo , albumartist_original:foo``.
    ``always`` (e.g. ``singleton:true``) is added to every alternative. Queries
    that use fields, ``,`` or sorting are left as they are.
    """
    always = always or []
    if not terms or any(":" in t or "," in t or t.endswith(("+", "-")) for t in terms):
        return terms + always
    out = terms + always
    for f in fields:
        out += [","] + [f"{f}:{t}" for t in terms] + always
    return out


def library_status(lib: Any | None = None) -> dict[str, Any]:
    """Album counts per source, and how many already have original names stored."""
    lib = lib or beets_ctx.lib()
    with lib.transaction() as tx:
        by_source = tx.query(
            "SELECT COALESCE(f.value, ''), COUNT(*) FROM albums a LEFT JOIN album_attributes f "
            "ON f.entity_id = a.id AND f.key = 'data_source' GROUP BY 1 ORDER BY 2 DESC"
        )
        done = tx.query(
            "SELECT COUNT(DISTINCT i.album_id) FROM items i JOIN item_attributes f "
            "ON f.entity_id = i.id AND f.key IN (?, ?, ?, ?) WHERE i.album_id IS NOT NULL",
            ORIGINAL_FIELDS,
        )[0][0]
    sources = [
        {"name": s or "unknown", "albums": n, "supported": s.lower() in SUPPORTED}
        for s, n in by_source
    ]
    return {
        "sources": sources,
        "supported": sum(s["albums"] for s in sources if s["supported"]),
        "romanized": done,
    }


# ----------------------------------------------------------------- jobs


def _album_plan(album: Any, items: list[Any], pair: tuple[Any, Any]) -> tuple[dict, dict]:
    """Field changes for a library album and its tracks (keyed by item id)."""
    roman, original = pair
    album_changes: dict[str, Any] = {}
    if roman.album and roman.album != original.album:
        album_changes.update(album=roman.album, album_original=original.album)
    if roman.artist and roman.artist != original.artist:
        album_changes.update(_artist_fields("albumartist", roman.artist))
        album_changes["albumartist_original"] = original.artist
    r_ids, r_list = _track_names(roman)
    o_ids, o_list = _track_names(original)
    ordered = sorted(items, key=lambda i: (i.disc or 0, i.track or 0))
    item_changes: dict[int, dict[str, Any]] = {}
    for i, item in enumerate(ordered):
        probe = {"track_id": item.mb_trackid}
        r, o = _match(probe, i, r_ids, r_list), _match(probe, i, o_ids, o_list)
        if r is None or o is None:
            continue
        ch: dict[str, Any] = {}
        if r.title and r.title != o.title:
            ch.update(title=r.title, title_original=o.title)
        if r.artist and r.artist != o.artist:
            ch.update(_artist_fields("artist", r.artist))
            ch["artist_original"] = o.artist
        if ch:
            item_changes[item.id] = ch
    return album_changes, item_changes


def _final_albumartist(
    name: str, original: str, settings: Any, aliases: dict[str, str], lib: Any
) -> str:
    """Apply the primary-artist and alias rules to a romanized album artist."""
    from . import artists

    known = artists.known_artists(lib, aliases)
    if settings.primary_artist and (primary := artists.primary_of(name, None, known)):
        name = primary
    return aliases.get(name.casefold()) or aliases.get(original.casefold()) or name


def _move_artist_images(moves: set[tuple[Path, Path]], names: list[str], ctx: JobContext):
    """Take artist pictures along when an artist folder is renamed."""
    for old, new in moves:
        if old == new or not old.is_dir() or not new.is_dir():
            continue
        for name in names:
            if (old / name).is_file() and not (new / name).exists():
                shutil.move(str(old / name), str(new / name))
                ctx.log(f"moved artist picture {old / name} -> {new / name}")


def register_jobs(jobs: JobQueue, settings: Any) -> None:
    @jobs.register("romanize")
    def romanize(ctx: JobContext) -> None:
        from .artists import prune_dirs

        lib = beets_ctx.lib()
        write, move = bool(ctx.params.get("write")), bool(ctx.params.get("move"))
        only_new = bool(ctx.params.get("only_new"))
        aliases = ctx.state.artist_aliases()
        root = Path(os.fsdecode(lib.directory))
        snap = Snapshot(ctx, "Romanize names")
        ids = ctx.params.get("album_ids")
        albums = [lib.get_album(int(i)) for i in ids] if ids else list(lib.albums())
        done = unsupported = without = failed = 0
        old_dirs: set[Path] = set()
        artist_dirs: set[tuple[Path, Path]] = set()
        for album in albums:
            if album is None:
                continue
            source = str(album.get("data_source") or "")
            name = f"{album.albumartist} - {album.album}"
            if source.lower() not in SUPPORTED or not album.mb_albumid:
                unsupported += 1
                continue
            items = list(album.items())
            if only_new and any(k in i for i in items for k in ORIGINAL_FIELDS):
                continue
            try:
                pair = fetch_pair(source, album.mb_albumid)
            except Exception as e:  # noqa: BLE001 - one album must not stop the job
                failed += 1
                ctx.log(f"{name}: lookup failed ({type(e).__name__}: {e})")
                continue
            if pair is None:
                without += 1
                ctx.log(f"{name}: {source} has no romanized names")
                continue
            album_changes, item_changes = _album_plan(album, items, pair)
            if "albumartist" in album_changes:
                final = _final_albumartist(
                    album_changes["albumartist"],
                    album_changes.get("albumartist_original", ""),
                    settings,
                    aliases,
                    lib,
                )
                album_changes.update(_artist_fields("albumartist", final))
            album_changes = {k: v for k, v in album_changes.items() if album.get(k) != v}
            item_changes = {
                iid: {k: v for k, v in ch.items() if lib.get_item(iid).get(k) != v}
                for iid, ch in item_changes.items()
            }
            if not album_changes and not any(item_changes.values()):
                ctx.log(f"{name}: already romanized")
                continue
            before = {i.id: i.filepath.parent.parent for i in items}
            for item in items:
                old_dirs.update({item.filepath.parent, item.filepath.parent.parent})
            ctx.log(
                f"{name} -> {album_changes.get('albumartist', album.albumartist)} - "
                f"{album_changes.get('album', album.album)}"
            )
            snap.capture_album(album, album_changes)
            album.update(album_changes)
            album.store()
            for item in items:
                ch = item_changes.get(item.id) or {}
                snap.capture(item, ch)
                item.load()
                item.update(ch)
                item.store()
            album.try_sync(write=write, move=move)
            for item in album.items():
                if item.id in before:
                    artist_dirs.add((before[item.id], item.filepath.parent.parent))
            done += 1
        snap.save(write, move)
        if move:
            _move_artist_images(artist_dirs, settings.artist_image_names, ctx)
            for d in prune_dirs(old_dirs, root, settings.artist_image_names):
                ctx.log(f"removed empty folder {d}")
        ctx.log(
            f"Romanized {done} album(s); {without} without romanized names at the source, "
            f"{unsupported} from other sources (not supported)"
            + (f", {failed} lookup(s) failed" if failed else "")
        )

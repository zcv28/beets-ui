"""Turn beets match objects into plain JSON-serialisable dicts for the UI."""

from __future__ import annotations

import os
from typing import Any

from beets.autotag import AlbumMatch, TrackMatch


def _s(v: Any) -> Any:
    if isinstance(v, bytes):
        return os.fsdecode(v)
    return v


def task_key(paths: list[bytes] | list[str]) -> str:
    """Stable identifier for an import task: its sorted file/dir paths."""
    return "\n".join(sorted(_s(p) for p in paths))


def item_dict(item: Any) -> dict[str, Any]:
    return {
        "path": _s(item.path),
        "filename": os.path.basename(_s(item.path)),
        "title": item.title,
        "artist": item.artist,
        "album": item.album,
        "albumartist": item.albumartist,
        "track": item.track,
        "disc": item.disc,
        "length": round(float(item.length or 0), 1),
        "format": item.format,
        "bitrate": int(item.bitrate or 0) // 1000,
        "year": item.year,
    }


def _distance(dist: Any) -> dict[str, Any]:
    d = float(dist)
    return {
        "distance": round(d, 4),
        "match": round((1 - d) * 100, 1),
        "penalties": list(dist.generic_penalty_keys),
    }


def album_match_dict(m: AlbumMatch) -> dict[str, Any]:
    info = m.info
    tracks = []
    for item, track in sorted(
        m.mapping.items(), key=lambda p: (p[1].medium or 0, p[1].medium_index or p[1].index or 0)
    ):
        cur_title = item.title or ""
        new_title = track.title or ""
        cur_len = float(item.length or 0)
        new_len = float(track.length or 0)
        tracks.append(
            {
                "path": _s(item.path),
                "cur_title": cur_title,
                "new_title": new_title,
                "cur_track": item.track,
                "new_track": track.medium_index or track.index,
                "medium": track.medium,
                "cur_length": round(cur_len, 1),
                "new_length": round(new_len, 1),
                "new_artist": track.artist,
                "title_changed": cur_title.strip() != new_title.strip(),
                "length_off": bool(new_len and abs(cur_len - new_len) > 10),
            }
        )
    try:
        disambig = m.disambig_string
    except Exception:  # noqa: BLE001 - disambig formatting is cosmetic
        disambig = ""
    # beets prints missing fields as "None" and repeats what the page already shows.
    shown = {
        str(v)
        for v in (
            info.data_source,
            info.year,
            info.label,
            info.catalognum,
            info.country,
            info.media,
        )
        if v
    }
    disambig = ", ".join(
        p for p in disambig.split(", ") if p and p not in ("None", "0") and p not in shown
    )
    return {
        "kind": "album",
        "id": info.album_id,
        "source": info.data_source,
        "url": info.get("data_url"),
        "artist": info.artist,
        "album": info.album,
        "year": info.year,
        "original_year": info.original_year,
        "label": info.label,
        "catalognum": info.catalognum,
        "country": info.country,
        "media": info.media,
        "mediums": info.mediums,
        "albumtype": info.albumtype,
        "disambig": disambig,
        "track_count": len(info.tracks or []),
        "tracks": tracks,
        "extra_items": [os.path.basename(_s(i.path)) for i in m.extra_items],
        "extra_tracks": [t.title for t in m.extra_tracks],
        **_distance(m.distance),
    }


def track_match_dict(m: TrackMatch) -> dict[str, Any]:
    info = m.info
    return {
        "kind": "track",
        "id": info.track_id,
        "source": info.data_source,
        "url": info.get("data_url"),
        "artist": info.artist,
        "title": info.title,
        "album": info.get("album"),
        "length": round(float(info.length or 0), 1),
        **_distance(m.distance),
    }


def match_dict(m: Any) -> dict[str, Any]:
    if isinstance(m, AlbumMatch):
        return album_match_dict(m)
    return track_match_dict(m)


def task_dict(task: Any) -> dict[str, Any]:
    """Serialise an import task after candidate lookup."""
    items = [item_dict(i) for i in task.items]
    src = task.source
    candidates = [match_dict(c) for c in (task.candidates or [])]
    return {
        "key": task_key(task.paths if task.is_album else [task.item.path]),
        "is_album": bool(task.is_album),
        "paths": [_s(p) for p in task.paths],
        "cur_artist": src.artist,
        "cur_album": src.name,
        "items": items,
        "candidates": candidates,
        "rec": task.rec.name if task.rec is not None else "none",
        "best": candidates[0]["match"] if candidates else None,
    }

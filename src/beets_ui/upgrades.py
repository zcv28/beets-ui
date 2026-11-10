"""Albums worth replacing with a better copy.

Read straight from the beets library, so the list is always current:

* ``lossy``: tracks in a lossy format (MP3, AAC, Vorbis, Opus, WMA, ...).
* ``bitrate``: lossy tracks below a usable bitrate (a subset of ``lossy``).
* ``incomplete``: fewer tracks than the track or disc totals say.
* ``mixed``: tracks in more than one format.

Low-resolution and missing covers are already listed on the Artwork page.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .state import State

LOSSY = {"MP3", "AAC", "OGG", "OPUS", "WINDOWS MEDIA", "MUSEPACK"}
# Average bitrate (kbps) below which a lossy album is worth replacing.
LOW_KBPS = {"OPUS": 96}
LOW_KBPS_DEFAULT = 192

REASONS = {
    "lossy": "Lossy",
    "bitrate": "Low bitrate",
    "incomplete": "Incomplete",
    "mixed": "Mixed formats",
}


def dismiss_key(album_id: int, reason: str) -> str:
    return f"{album_id}:{reason}"


def _albums(lib: Any) -> tuple[dict[int, dict], dict[int, list[tuple]]]:
    with lib.transaction() as tx:
        albums = {
            r[0]: {"id": r[0], "albumartist": r[1], "album": r[2], "year": r[3]}
            for r in tx.query("SELECT id, albumartist, album, year FROM albums")
        }
        items: dict[int, list[tuple]] = defaultdict(list)
        for r in tx.query(
            "SELECT album_id, format, bitrate, track, tracktotal, disc, disctotal "
            "FROM items WHERE album_id IS NOT NULL"
        ):
            items[r[0]].append(r[1:])
    return albums, items


def _issues(tracks: list[tuple]) -> dict[str, str]:
    out: dict[str, str] = {}
    formats: dict[str, list[int]] = defaultdict(list)
    for fmt, bitrate, *_ in tracks:
        formats[(fmt or "?").upper()].append(int(bitrate or 0))
    lossy = {f: rates for f, rates in formats.items() if f in LOSSY}
    if lossy:
        parts = []
        low = False
        for f, rates in sorted(lossy.items()):
            kbps = round(sum(rates) / len(rates) / 1000) if rates else 0
            parts.append(f"{f} {kbps} kbps" if kbps else f)
            low |= bool(kbps) and kbps < LOW_KBPS.get(f, LOW_KBPS_DEFAULT)
        out["lossy"] = ", ".join(parts)
        if low:
            out["bitrate"] = out["lossy"]
    if len(formats) > 1:
        out["mixed"] = ", ".join(f"{f} ×{len(r)}" for f, r in sorted(formats.items()))

    discs: dict[int, list[tuple]] = defaultdict(list)
    for t in tracks:
        discs[int(t[4] or 1)].append(t)
    missing = 0
    for disc_tracks in discs.values():
        total = max(int(t[3] or 0) for t in disc_tracks)
        numbers = {int(t[2] or 0) for t in disc_tracks}
        if total:
            missing += max(0, total - len(numbers - {0}))
    disctotal = max(int(t[5] or 0) for t in tracks)
    gone_discs = max(0, disctotal - len(discs)) if disctotal else 0
    if missing or gone_discs:
        bits = []
        if missing:
            bits.append(f"{missing} track{'s' if missing != 1 else ''} missing")
        if gone_discs:
            bits.append(f"{gone_discs} disc{'s' if gone_discs != 1 else ''} missing")
        out["incomplete"] = ", ".join(bits)
    return out


def find(lib: Any, state: State, reason: str = "") -> dict[str, Any]:
    """Albums with issues (``reason`` narrows to one kind) and counts per kind."""
    albums, items = _albums(lib)
    dismissed = state.dismissed("upgrade")
    counts = dict.fromkeys(REASONS, 0)
    hidden = 0
    rows = []
    for album_id, tracks in items.items():
        a = albums.get(album_id)
        if a is None:
            continue
        issues = {
            k: v for k, v in _issues(tracks).items() if dismiss_key(album_id, k) not in dismissed
        }
        hidden += sum(1 for k in REASONS if dismiss_key(album_id, k) in dismissed)
        for k in issues:
            counts[k] += 1
        if issues and (not reason or reason in issues):
            rows.append({**a, "issues": issues, "tracks": len(tracks)})
    rows.sort(key=lambda r: ((r["albumartist"] or "").casefold(), r["year"] or 0, r["album"]))
    return {"rows": rows, "counts": counts, "dismissed": hidden}

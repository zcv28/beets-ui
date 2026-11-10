"""Pick a copy: score the offers from a search against the album that was asked for.

An offer is *confident* only when beets-ui is sure enough to download it without
asking: the album's name is in its path, it has the expected tracks (or, when the
tracklist is unknown, the artist's name is in the path too), and it is good
enough for what was asked (lossless, or better than the copy it replaces). The
rest are still ranked, best first, for you to choose from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .discography import norm

COMPILATION_WORDS = ("various", "variousartists", "va", "omnibus", "compilation")


@dataclass
class Pick:
    offer: dict[str, Any]
    score: float
    confident: bool
    why: list[str] = field(default_factory=list)


def _names(*values: str) -> list[str]:
    out = []
    for v in values:
        n = norm(v)
        if len(n) >= 2 and n not in out:
            out.append(n)
    return out


def _bitrate(offer: dict[str, Any]) -> int:
    return int(offer.get("bitrate") or 0)


def score(offer: dict[str, Any], want: dict[str, Any]) -> Pick:
    """How well one offer fits the request, and whether to take it unasked."""
    path = norm(offer.get("folder") or offer.get("path") or "")
    why: list[str] = []
    albums = _names(want.get("album", ""), want.get("album_original", ""))
    artists = _names(want.get("artist", ""), want.get("artist_original", ""))
    album_hit = any(a in path for a in albums)
    artist_hit = any(a in path for a in artists)
    s = 0.0
    if album_hit:
        s += 10
    else:
        why.append("album name not in the folder")
    if artist_hit:
        s += 3

    tracks = want.get("tracks") or []
    expected = len(tracks)
    n = int(offer.get("tracks") or 0)
    count_ok = False
    if expected:
        if n == expected:
            s += 6
            count_ok = True
        elif n == expected + 1:
            s += 3  # a bonus track
            count_ok = True
        elif n < expected:
            s -= 10
            why.append(f"{n} of {expected} tracks")
        else:
            s -= 3
            why.append(f"{n} tracks, expected {expected}")
        total = sum(t.get("length") or 0 for t in tracks)
        got = sum(f.get("length") or 0 for f in offer.get("files", []) if f.get("audio", True))
        if total and got and count_ok:
            if abs(got - total) <= max(0.03 * total, 10):
                s += 3
            else:
                s -= 4
                count_ok = False
                why.append("track lengths differ")

    lossless = bool(offer.get("lossless"))
    rate = _bitrate(offer)
    quality_ok = True
    if lossless:
        s += 4
    elif rate >= 320:
        s += 2
    elif rate >= 256:
        s += 1
    if want.get("want_lossless") and not lossless:
        quality_ok = False
        why.append("not lossless")
    min_rate = int(want.get("min_bitrate") or 0)
    if min_rate and not lossless and rate <= min_rate:
        quality_ok = False
        why.append(f"{rate or '?'} kbps is no better than {min_rate} kbps")

    if offer.get("free_slot"):
        s += 2
    s -= min(int(offer.get("queue") or 0) / 10, 3)
    s += min(int(offer.get("speed") or 0) / 1_000_000, 2)
    if offer.get("locked"):
        s -= 5
        why.append("some files are locked")
    if not artist_hit and any(w in path for w in COMPILATION_WORDS):
        s -= 2

    if expected:
        confident = album_hit and count_ok and quality_ok
    else:
        confident = (
            album_hit and artist_hit and quality_ok and len(albums[0] if albums else "") >= 4
        )
        if album_hit and not confident and quality_ok:
            why.append("tracklist unknown and the artist isn't named")
    return Pick(offer, s, confident and not offer.get("locked"), why)


def rank(offers: list[dict[str, Any]], want: dict[str, Any]) -> list[Pick]:
    """Every offer that names the album, best first (confident ones before the rest)."""
    picks = [score(o, want) for o in offers]
    picks = [p for p in picks if "album name not in the folder" not in p.why]
    picks.sort(key=lambda p: (not p.confident, -p.score))
    return picks


def audio_count(files: list[dict[str, Any]]) -> int:
    return sum(1 for f in files if f.get("audio", True))


# ----------------------------------------------------------- searches by hand


def _search_score(offer: dict[str, Any], words: list[str]) -> float:
    path = norm(offer.get("folder") or offer.get("path") or "")
    s = 6 * (sum(1 for w in words if w in path) / len(words)) if words else 0.0
    rate = _bitrate(offer)
    if offer.get("lossless"):
        s += 4
    elif rate >= 320:
        s += 2
    elif rate >= 256:
        s += 1
    if offer.get("free_slot"):
        s += 2
    s -= min(int(offer.get("queue") or 0) / 10, 3)
    s += min(int(offer.get("speed") or 0) / 1_000_000, 2)
    if offer.get("locked"):
        s -= 5
    return s


def strengths(offer: dict[str, Any]) -> list[str]:
    """What speaks for a copy, in a few words: quality, then how soon it arrives."""
    out = []
    rate = _bitrate(offer)
    if offer.get("lossless"):
        out.append("lossless")
    elif rate >= 320:
        out.append(f"{rate} kbps")
    if offer.get("free_slot"):
        out.append("free upload slot")
    elif int(offer.get("queue") or 0) <= 5:
        out.append("short queue")
    if int(offer.get("speed") or 0) >= 1_000_000:
        out.append("fast")
    return out


def for_search(offers: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """A search by hand, best copy first: the words searched for, quality, availability."""
    words = [w for w in (norm(t) for t in text.split()) if len(w) >= 2]
    return sorted(offers, key=lambda o: -_search_score(o, words))

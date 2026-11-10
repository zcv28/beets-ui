"""Discography gaps: releases by artists in the library that the library lacks.

Each album artist is looked up where its albums were tagged from:

* VocaDB, TouhouDB, UtaiteDB: the artist's own releases (``OnlyMainAlbums``),
  found by the artist ID the beets-vocadb plugin stores in
  ``mb_albumartistid``.
* MusicBrainz: the artist's release groups.

Lookups are cached in beets-ui's state database and refreshed by a job.
Which releases are owned is worked out when the page is shown, so new
imports count straight away: by release ID, or else by title among the
artist's albums.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from typing import Any

import httpx

from . import __version__, beets_ctx
from .jobs import JobContext, JobQueue
from .state import State

log = logging.getLogger("beets_ui.discography")

USER_AGENT = f"beets-ui/{__version__} (+https://github.com/zcv28/beets-ui)"
VOCADB = {
    "vocadb": "https://vocadb.net",
    "touhoudb": "https://touhoudb.com",
    "utaitedb": "https://utaitedb.net",
}
MUSICBRAINZ = "https://musicbrainz.org"
MB_VARIOUS = "89ad4ac3-39f7-470e-963a-56509c546377"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SOURCE_NAMES = {"vocadb": "VocaDB", "touhoudb": "TouhouDB", "utaitedb": "UtaiteDB"}
SOURCE_NAMES["musicbrainz"] = "MusicBrainz"

# Release kinds shown as filters, in order.
KINDS = ("album", "ep", "single", "compilation", "other")
DEFAULT_KINDS = ("album", "ep")
VOCADB_KINDS = {
    "Album": "album",
    "SplitAlbum": "album",
    "Instrumental": "album",
    "Fanmade": "album",
    "EP": "ep",
    "Single": "single",
    "Compilation": "compilation",
}
MB_OTHER = {
    "Live",
    "Remix",
    "DJ-mix",
    "Demo",
    "Interview",
    "Spokenword",
    "Audiobook",
    "Audio drama",
    "Field recording",
    "Mixtape/Street",
}
PAGE = 100
MAX_PAGES = 20


class DiscographyError(RuntimeError):
    pass


def norm(title: str) -> str:
    """A title reduced to letters and digits, for matching across sources."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", title or "").casefold())


# ---------------------------------------------------------------- library


def owned_index(lib: Any) -> dict[str, set]:
    """What the library has, to tell whether an album asked for is in it.

    "ids": release group IDs (MusicBrainz) and album IDs (VocaDB, TouhouDB,
    UtaiteDB and MusicBrainz releases); "pairs": (artist, album) names,
    normalised, romanized and original.
    """
    ids: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for a in lib.albums():
        for f in ("mb_releasegroupid", "mb_albumid"):
            if a.get(f):
                ids.add(str(a[f]))
        names = {norm(a.album), norm(a.get("album_original") or "")} - {""}
        artists = {norm(a.albumartist), norm(a.get("albumartist_original") or "")} - {""}
        pairs |= {(ar, n) for ar in artists for n in names}
    return {"ids": ids, "pairs": pairs}


def is_owned(index: dict[str, set], ids: list[str], artists: list[str], albums: list[str]) -> bool:
    if any(i and i in index["ids"] for i in ids):
        return True
    ars = {norm(x) for x in artists} - {""}
    als = {norm(x) for x in albums} - {""}
    return any((ar, al) in index["pairs"] for ar in ars for al in als)


def source_key(album: Any) -> tuple[str, str] | None:
    """(source, artist ID) an album's artist is looked up by, if any."""
    src = str(album.get("data_source") or "").lower()
    aid = str(album.get("mb_albumartistid") or "").strip()
    if not aid or album.get("comp"):
        return None
    if src in VOCADB:
        return (src, aid) if aid.isdigit() else None
    if src in ("musicbrainz", "") and UUID.match(aid) and aid != MB_VARIOUS:
        return ("musicbrainz", aid)
    return None


def library_groups(lib: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """Every (source, artist ID) in the library with the releases it owns."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    titles_by_artist: dict[str, set[str]] = {}
    for a in lib.albums():
        names = {norm(a.album), norm(a.get("album_original") or "")} - {""}
        for artist in (a.albumartist, a.get("albumartist_original") or ""):
            if artist:
                titles_by_artist.setdefault(artist.casefold(), set()).update(names)
        key = source_key(a)
        if key is None:
            continue
        g = groups.setdefault(
            key,
            {
                "source": key[0],
                "artist_id": key[1],
                "artist": a.albumartist,
                "artist_original": a.get("albumartist_original") or "",
                "ids": set(),
                "titles": set(),
                "names": set(),
                "albums": 0,
            },
        )
        g["albums"] += 1
        g["names"].add(a.albumartist.casefold())
        if a.get("albumartist_original"):
            g["names"].add(a.albumartist_original.casefold())
        g["titles"].update(names)
        rid = a.mb_releasegroupid if key[0] == "musicbrainz" else a.mb_albumid
        if rid:
            g["ids"].add(str(rid))
    for g in groups.values():
        for name in g.pop("names"):
            g["titles"].update(titles_by_artist.get(name, ()))
    return groups


# ---------------------------------------------------------------- lookups


class Client:
    """HTTP client that keeps to each site's request rate."""

    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30,
            follow_redirects=True,
        )
        self._last: dict[str, float] = {}

    def get(self, url: str, params: dict[str, Any], interval: float) -> Any:
        host = httpx.URL(url).host
        wait = self._last.get(host, 0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            self._last[host] = time.monotonic()
            r = self.http.get(url, params=params)
            if r.status_code in (429, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 404:
                raise DiscographyError("artist not found")
            r.raise_for_status()
            return r.json()
        raise DiscographyError("rate limited")  # pragma: no cover - loop always returns


def _date(d: dict | None) -> str:
    if not d or d.get("isEmpty") or not d.get("year"):
        return ""
    parts = [f"{d['year']:04d}"]
    if d.get("month"):
        parts.append(f"{d['month']:02d}")
        if d.get("day"):
            parts.append(f"{d['day']:02d}")
    return "-".join(parts)


def fetch_vocadb(client: Client, source: str, artist_id: str) -> list[dict[str, Any]]:
    base = VOCADB[source]
    out: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        data = client.get(
            f"{base}/api/albums",
            {
                "artistId[]": artist_id,
                "artistParticipationStatus": "OnlyMainAlbums",
                "maxResults": PAGE,
                "start": page * PAGE,
                "getTotalCount": "true",
                "lang": "Romaji",
                "fields": "AdditionalNames",
                "sort": "ReleaseDate",
            },
            interval=0.5,
        )
        for it in data.get("items", []):
            names = [it.get("name") or "", it.get("defaultName") or ""]
            names += [n.strip() for n in (it.get("additionalNames") or "").split(", ")]
            out.append(
                {
                    "id": str(it["id"]),
                    "title": it.get("name") or it.get("defaultName") or "",
                    "original": it.get("defaultName") or "",
                    "kind": VOCADB_KINDS.get(it.get("discType") or "", "other"),
                    "type": it.get("discType") or "",
                    "date": _date(it.get("releaseDate")),
                    "catalog": it.get("catalogNumber") or "",
                    "artists": it.get("artistString") or "",
                    "url": f"{base}/Al/{it['id']}",
                    "names": sorted({n for n in names if n}),
                }
            )
        if (page + 1) * PAGE >= int(data.get("totalCount") or 0):
            break
    out.sort(key=lambda r: r["date"] or "9999")
    return out


def _mb_kind(primary: str, secondary: list[str]) -> str:
    if "Compilation" in secondary:
        return "compilation"
    if MB_OTHER.intersection(secondary):
        return "other"
    return {"Album": "album", "EP": "ep", "Single": "single"}.get(primary, "other")


def fetch_musicbrainz(client: Client, artist_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        data = client.get(
            f"{MUSICBRAINZ}/ws/2/release-group",
            {"artist": artist_id, "fmt": "json", "limit": PAGE, "offset": page * PAGE},
            interval=1.1,
        )
        for rg in data.get("release-groups", []):
            primary = rg.get("primary-type") or ""
            secondary = rg.get("secondary-types") or []
            out.append(
                {
                    "id": rg["id"],
                    "title": rg.get("title") or "",
                    "original": "",
                    "kind": _mb_kind(primary, secondary),
                    "type": " + ".join([primary or "Other", *secondary]),
                    "date": rg.get("first-release-date") or "",
                    "catalog": "",
                    "artists": "",
                    "url": f"{MUSICBRAINZ}/release-group/{rg['id']}",
                    "names": [rg.get("title") or ""],
                }
            )
        if (page + 1) * PAGE >= int(data.get("release-group-count") or 0):
            break
    out.sort(key=lambda r: r["date"] or "9999")
    return out


def fetch(client: Client, source: str, artist_id: str) -> list[dict[str, Any]]:
    if source in VOCADB:
        return fetch_vocadb(client, source, artist_id)
    if source == "musicbrainz":
        return fetch_musicbrainz(client, artist_id)
    raise DiscographyError(f"unknown source {source}")


# ------------------------------------------------------------------ view


def dismiss_key(source: str, release_id: str) -> str:
    return f"{source}:{release_id}"


def gaps(lib: Any, state: State, kinds: tuple[str, ...] = DEFAULT_KINDS) -> list[dict[str, Any]]:
    """One row per looked-up artist: its releases of ``kinds``, owned or missing."""
    groups = library_groups(lib)
    cached = {(d["source"], d["artist_id"]): d for d in state.discographies()}
    dismissed = state.dismissed("release")
    today = time.strftime("%Y-%m-%d")
    rows = []
    for key, g in groups.items():
        d = cached.get(key)
        row = {
            **{k: g[k] for k in ("source", "artist_id", "artist", "artist_original", "albums")},
            "source_name": SOURCE_NAMES.get(g["source"], g["source"]),
            "looked_up": d is not None and d["updated_at"],
            "error": d["error"] if d else None,
            "owned": [],
            "missing": [],
            "dismissed": [],
            "upcoming": [],
        }
        for r in d["releases"] if d else []:
            if r["kind"] not in kinds:
                continue
            owned = r["id"] in g["ids"] or any(norm(n) in g["titles"] for n in r["names"])
            if owned:
                row["owned"].append(r)
            elif dismiss_key(g["source"], r["id"]) in dismissed:
                row["dismissed"].append(r)
            elif r["date"] and r["date"] > today:
                row["upcoming"].append(r)
            else:
                row["missing"].append(r)
        rows.append(row)
    rows.sort(key=lambda r: (r["artist"].casefold(), r["source"]))
    return rows


def artist_gaps(
    lib: Any, state: State, name: str, kinds: tuple[str, ...] = DEFAULT_KINDS
) -> list[dict[str, Any]]:
    """gaps() for one album artist, by its name as shown or as written (one row per source)."""
    key = name.casefold()
    return [
        r
        for r in gaps(lib, state, kinds)
        if key in (r["artist"].casefold(), (r["artist_original"] or "").casefold())
    ]


# ------------------------------------------------------------------- job


def register_jobs(jobs: JobQueue, client_factory=Client) -> None:
    @jobs.register("discography", lane="net")
    def discography(ctx: JobContext) -> None:
        groups = library_groups(beets_ctx.lib())
        wanted = ctx.params.get("keys")
        keys = [tuple(k) for k in wanted] if wanted else sorted(groups)
        if not wanted:
            ctx.state.prune_discography(set(groups))
        client = client_factory()
        ctx.log(f"Looking up the discographies of {len(keys)} artist(s)")
        ok = 0
        for source, artist_id in keys:
            g = groups.get((source, artist_id))
            if g is None:
                ctx.log(f"  {source} {artist_id}: no longer in the library")
                continue
            label = f"{g['artist']} ({SOURCE_NAMES.get(source, source)})"
            try:
                releases = fetch(client, source, artist_id)
            except (httpx.HTTPError, DiscographyError, ValueError) as e:
                ctx.log(f"  {label}: {e}")
                ctx.state.set_discography(source, artist_id, g["artist"], [], error=str(e))
                continue
            ctx.state.set_discography(source, artist_id, g["artist"], releases)
            ok += 1
            ctx.log(f"  {label}: {len(releases)} release(s)")
        ctx.log(f"Done: {ok} of {len(keys)} looked up")

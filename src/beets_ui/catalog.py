"""Looking music up where releases are catalogued: MusicBrainz, VocaDB, TouhouDB, UtaiteDB.

Find searches these for artists, releases and tracks (not the download source),
and any artist or release found gets a page of its own: an artist's whole
discography to get all or some of, a release's tracklist to get it. Every release
here is one the Get engine can fetch: a MusicBrainz release group, or a VocaDB
(TouhouDB, UtaiteDB) album.

One rate-limited client is shared by all pages, so lookups from several page
parts at once queue politely (MusicBrainz allows one request a second). Answers
are kept in memory for a while; artists' discographies go in the state database,
next to the ones Missing looks up.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from . import discography
from .discography import MB_VARIOUS, MUSICBRAINZ, UUID, VOCADB, DiscographyError
from .state import State

SOURCES = ("musicbrainz", "vocadb", "touhoudb", "utaitedb")
NAMES = discography.SOURCE_NAMES
KINDS = ("artists", "releases", "tracks")
DAY = 86400
SEARCH_KEEP = 600
LOOKUP_KEEP = 3600
DIGITS = re.compile(r"^\d{1,12}$")
# Characters with a meaning in MusicBrainz's (Lucene) search syntax.
LUCENE = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')
# Who a VocaDB album or song is "by", in this order: the circle, then its producers.
VOCADB_MAIN = ("Circle", "Producer", "Band")


class CatalogError(RuntimeError):
    pass


def valid(source: str, ident: str) -> bool:
    """Whether an ID can belong to this source (pages are addressed by them)."""
    if source == "musicbrainz":
        return bool(UUID.match(ident or ""))
    return source in VOCADB and bool(DIGITS.match(ident or ""))


def reason(e: Exception) -> str:
    """What went wrong asking a catalogue, said plainly for the page."""
    if isinstance(e, DiscographyError):
        # The shared client calls every 404 a missing artist.
        return "not found" if "not found" in str(e) else str(e)
    if isinstance(e, httpx.HTTPStatusError):
        r = e.response
        return f"{r.request.url.host} answered {r.status_code} {r.reason_phrase}".strip()
    if isinstance(e, httpx.TimeoutException):
        return "it took too long to answer"
    if isinstance(e, httpx.HTTPError):
        return f"it couldn't be reached ({e.__class__.__name__})"
    return "its answer couldn't be read"


class _Shared:
    """discography.Client, one request at a time (it keeps to each site's rate)."""

    def __init__(self, http: httpx.Client | None = None):
        self._client = discography.Client(http)
        self._lock = threading.Lock()

    def get(self, url: str, params: dict[str, Any], interval: float) -> Any:
        with self._lock:
            return self._client.get(url, params, interval)


# ------------------------------------------------------------ MusicBrainz


def _mb_credit(credit: list[dict] | None) -> tuple[str, list[dict[str, str]]]:
    """(the credit as printed, the artists in it)."""
    credit = credit or []
    name = "".join((c.get("name") or "") + (c.get("joinphrase") or "") for c in credit)
    artists = [
        {"id": c["artist"]["id"], "name": c["artist"].get("name") or c.get("name") or ""}
        for c in credit
        if (c.get("artist") or {}).get("id")
    ]
    return name.strip(), artists


def _latin(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text)) and not re.search(r"[぀-ヿ㐀-鿿]", text)


def _mb_artist(a: dict[str, Any]) -> dict[str, Any]:
    """An artist from MusicBrainz, named in Latin letters when only their sort name is
    (ヨルシカ is Yorushika), the name as written kept next to it, as the library does."""
    name, sort = a.get("name") or "", a.get("sort-name") or ""
    roman = sort if sort and not _latin(name) and _latin(sort) else ""
    return {
        "source": "musicbrainz",
        "id": a["id"],
        "name": roman or name,
        "original": name if roman else "",
        "detail": ", ".join(
            x for x in (a.get("disambiguation"), a.get("type"), a.get("country")) if x
        ),
    }


def _mb_group(rg: dict[str, Any], credit: list[dict] | None = None) -> dict[str, Any]:
    primary = rg.get("primary-type") or ""
    secondary = rg.get("secondary-types") or []
    name, artists = _mb_credit(credit if credit is not None else rg.get("artist-credit"))
    title = rg.get("title") or ""
    return {
        "source": "musicbrainz",
        "id": rg["id"],
        "title": title,
        "original": "",
        "artist": name,
        "artists": artists,
        "date": rg.get("first-release-date") or "",
        "type": " + ".join([primary or "Other", *secondary]),
        "kind": discography._mb_kind(primary, secondary),
        "catalog": "",
        "url": f"{MUSICBRAINZ}/release-group/{rg['id']}",
        "names": [title],
    }


def _mb_release_of(rec: dict[str, Any]) -> dict[str, Any] | None:
    """The release group a recording is best known from: an official album or EP before
    a single, anything before a compilation, the earliest first."""

    def rank(r: dict[str, Any]) -> tuple:
        rg = r.get("release-group") or {}
        secondary = rg.get("secondary-types") or []
        return (
            (r.get("status") or "Official") != "Official",
            bool(secondary),
            {"Album": 0, "EP": 1, "Single": 2}.get(rg.get("primary-type") or "", 3),
            r.get("date") or "9999",
        )

    releases = [r for r in rec.get("releases") or [] if (r.get("release-group") or {}).get("id")]
    if not releases:
        return None
    best = min(releases, key=rank)
    rg = {"title": best.get("title") or "", **best["release-group"]}
    rel = _mb_group(rg, best.get("artist-credit") or rec.get("artist-credit"))
    rel["date"] = rel["date"] or best.get("date") or ""
    return rel


def _mb_query(text: str) -> str:
    return LUCENE.sub(r"\\\1", text.strip())


# ------------------------------------------------------------------ VocaDB


def _vdb_main(entries: list[dict] | None) -> list[dict[str, str]]:
    """The main artists of a VocaDB album or song: its circle, else its producers."""
    entries = [
        e for e in entries or [] if not e.get("isSupport") and (e.get("artist") or {}).get("id")
    ]
    for cat in VOCADB_MAIN:
        found = [e for e in entries if cat in (e.get("categories") or "")]
        if found:
            entries = found
            break
    return [{"id": str(e["artist"]["id"]), "name": e["artist"].get("name") or ""} for e in entries]


def _vdb_album(source: str, it: dict[str, Any]) -> dict[str, Any]:
    names = [it.get("name") or "", it.get("defaultName") or ""]
    names += [n.strip() for n in (it.get("additionalNames") or "").split(", ")]
    title = it.get("name") or it.get("defaultName") or ""
    original = it.get("defaultName") or ""
    return {
        "source": source,
        "id": str(it["id"]),
        "title": title,
        "original": original if original != title else "",
        "artist": it.get("artistString") or "",
        "artists": _vdb_main(it.get("artists")),
        "date": discography._date(it.get("releaseDate")),
        "type": it.get("discType") or "",
        "kind": discography.VOCADB_KINDS.get(it.get("discType") or "", "other"),
        "catalog": it.get("catalogNumber") or "",
        "url": f"{VOCADB[source]}/Al/{it['id']}",
        "names": sorted({n for n in names if n}),
    }


def _vdb_album_of(source: str, song: dict[str, Any]) -> dict[str, Any] | None:
    def rank(a: dict[str, Any]) -> tuple:
        kind = discography.VOCADB_KINDS.get(a.get("discType") or "", "other")
        return (
            {"album": 0, "ep": 1, "single": 2}.get(kind, 3),
            discography._date(a.get("releaseDate")) or "9999",
        )

    albums = [a for a in song.get("albums") or [] if a.get("id")]
    return _vdb_album(source, min(albums, key=rank)) if albums else None


# ------------------------------------------------------------------ catalog


class Catalog:
    def __init__(self, state: State, http: httpx.Client | None = None):
        self.state = state
        self.client = _Shared(http)
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()

    def _get(self, url: str, params: dict[str, Any], interval: float) -> Any:
        try:
            return self.client.get(url, params, interval)
        except (DiscographyError, httpx.HTTPError, ValueError) as e:
            raise CatalogError(reason(e)) from e

    def _mb(self, path: str, params: dict[str, Any]) -> Any:
        return self._get(f"{MUSICBRAINZ}/ws/2/{path}", {**params, "fmt": "json"}, 1.1)

    def _vdb(self, source: str, path: str, params: dict[str, Any]) -> Any:
        return self._get(f"{VOCADB[source]}/api/{path}", {"lang": "Romaji", **params}, 0.5)

    def _kept(self, key: tuple, keep: float, fetch: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < keep:
                return hit[1]
        value = fetch()
        with self._cache_lock:
            if len(self._cache) > 500:
                self._cache = {k: v for k, v in self._cache.items() if now - v[0] < keep}
            self._cache[key] = (now, value)
        return value

    def _keep_release(self, rel: dict[str, Any]) -> None:
        """A release found by a search is remembered, so its page and Get need no lookup."""
        with self._cache_lock:
            self._cache.setdefault(("release", rel["source"], rel["id"]), (time.monotonic(), rel))

    # ------------------------------------------------------------ search

    def search(self, source: str, kind: str, text: str) -> list[dict[str, Any]]:
        """Artists, releases or tracks matching ``text``, best first."""
        if source not in SOURCES or kind not in KINDS:
            raise CatalogError(f"can't search {source} for {kind}")
        text = " ".join(text.split())
        found = self._kept(
            ("search", source, kind, text.casefold()),
            SEARCH_KEEP,
            lambda: (self._search_mb if source == "musicbrainz" else self._search_vdb)(
                source, kind, text
            ),
        )
        for r in found:
            rel = r if kind == "releases" else r.get("release") if kind == "tracks" else None
            if rel:
                self._keep_release(rel)
        return found

    def _search_mb(self, _source: str, kind: str, text: str) -> list[dict[str, Any]]:
        q = _mb_query(text)
        if kind == "artists":
            data = self._mb("artist", {"query": q, "limit": 8})
            return [
                _mb_artist(a)
                for a in data.get("artists") or []
                if a.get("id") and a["id"] != MB_VARIOUS
            ]
        # By its title or by who it is by ("yorushika elma" finds Elma by Yorushika first).
        if kind == "releases":
            data = self._mb(
                "release-group", {"query": f"releasegroup:({q}) OR artist:({q})", "limit": 12}
            )
            return [_mb_group(rg) for rg in data.get("release-groups") or [] if rg.get("id")]
        data = self._mb("recording", {"query": f"recording:({q}) OR artist:({q})", "limit": 12})
        out = []
        for rec in data.get("recordings") or []:
            name, artists = _mb_credit(rec.get("artist-credit"))
            out.append(
                {
                    "source": "musicbrainz",
                    "id": rec["id"],
                    "title": rec.get("title") or "",
                    "original": "",
                    "artist": name,
                    "artists": artists,
                    "length": round((rec.get("length") or 0) / 1000),
                    "release": _mb_release_of(rec),
                }
            )
        return out

    def _search_vdb(self, source: str, kind: str, text: str) -> list[dict[str, Any]]:
        common = {"query": text, "preferAccurateMatches": "true", "nameMatchMode": "Auto"}
        if kind == "artists":
            data = self._vdb(
                source, "artists", {**common, "maxResults": 8, "sort": "FollowerCount"}
            )
            out = []
            for a in data.get("items") or []:
                name = a.get("name") or a.get("defaultName") or ""
                original = a.get("defaultName") or ""
                out.append(
                    {
                        "source": source,
                        "id": str(a["id"]),
                        "name": name,
                        "original": original if original != name else "",
                        "detail": a.get("artistType") or "",
                    }
                )
            return out
        if kind == "releases":
            data = self._vdb(
                source,
                "albums",
                {**common, "maxResults": 12, "fields": "Artists,AdditionalNames"},
            )
            return [_vdb_album(source, it) for it in data.get("items") or []]
        data = self._vdb(source, "songs", {**common, "maxResults": 12, "fields": "Albums,Artists"})
        out = []
        for s in data.get("items") or []:
            title = s.get("name") or s.get("defaultName") or ""
            original = s.get("defaultName") or ""
            out.append(
                {
                    "source": source,
                    "id": str(s["id"]),
                    "title": title,
                    "original": original if original != title else "",
                    "artist": s.get("artistString") or "",
                    "artists": _vdb_main(s.get("artists")),
                    "length": int(s.get("lengthSeconds") or 0),
                    "release": _vdb_album_of(source, s),
                }
            )
        return out

    # ----------------------------------------------------------- lookups

    def release(self, source: str, release_id: str) -> dict[str, Any]:
        """A release group (MusicBrainz) or album (VocaDB and its sisters)."""
        if not valid(source, release_id):
            raise CatalogError("not a release ID")

        def fetch() -> dict[str, Any]:
            if source == "musicbrainz":
                return _mb_group(self._mb(f"release-group/{release_id}", {"inc": "artist-credits"}))
            data = self._vdb(source, f"albums/{release_id}", {"fields": "Artists,AdditionalNames"})
            return _vdb_album(source, data)

        return self._kept(("release", source, release_id), LOOKUP_KEEP, fetch)

    def peek_release(self, source: str, release_id: str) -> dict[str, Any] | None:
        """A release already looked up, without asking the catalogue."""
        with self._cache_lock:
            hit = self._cache.get(("release", source, release_id))
        return hit[1] if hit else None

    def tracks(self, source: str, release_id: str) -> list[dict[str, Any]]:
        """The release's tracklist (for MusicBrainz, of its usual release)."""
        from .acquire import tracklist

        if not valid(source, release_id):
            raise CatalogError("not a release ID")

        def fetch() -> list[dict[str, Any]]:
            try:
                return tracklist(self.client, source, release_id)[0]  # type: ignore[arg-type]
            except (DiscographyError, httpx.HTTPError, ValueError) as e:
                raise CatalogError(reason(e)) from e

        return self._kept(("tracks", source, release_id), LOOKUP_KEEP, fetch)

    def artist(self, source: str, artist_id: str) -> dict[str, Any]:
        """An artist's name (and, from VocaDB, as written) and a word on who they are."""
        if not valid(source, artist_id):
            raise CatalogError("not an artist ID")

        def fetch() -> dict[str, Any]:
            if source == "musicbrainz":
                a = _mb_artist(self._mb(f"artist/{artist_id}", {}))
                return {**a, "url": f"{MUSICBRAINZ}/artist/{artist_id}"}
            a = self._vdb(source, f"artists/{artist_id}", {})
            name = a.get("name") or a.get("defaultName") or ""
            original = a.get("defaultName") or ""
            return {
                "source": source,
                "id": artist_id,
                "name": name,
                "original": original if original != name else "",
                "detail": a.get("artistType") or "",
                "url": f"{VOCADB[source]}/Ar/{artist_id}",
            }

        return self._kept(("artist", source, artist_id), DAY, fetch)

    def main_artist(self, source: str, release_id: str) -> dict[str, str] | None:
        """The artist a release is by ({"id", "name"}), or None for various artists."""
        rel = self.release(source, release_id)
        return next((a for a in rel["artists"] if a["id"] != MB_VARIOUS), None)

    def discography(
        self, source: str, artist_id: str, name: str = "", fresh: bool = False
    ) -> tuple[list[dict[str, Any]], float]:
        """(the artist's releases, when they were looked up), from the state database when
        looked up within a day (by Missing, or by an earlier visit)."""
        if not valid(source, artist_id):
            raise CatalogError("not an artist ID")
        cached = self.state.discography(source, artist_id)
        if (
            cached
            and not fresh
            and not cached["error"]
            and time.time() - cached["updated_at"] < DAY
        ):
            return cached["releases"], cached["updated_at"]
        name = name or (cached or {}).get("artist") or self.artist(source, artist_id)["name"]
        try:
            releases = discography.fetch(self.client, source, artist_id)  # type: ignore[arg-type]
        except (DiscographyError, httpx.HTTPError, ValueError) as e:
            if cached and cached["releases"]:
                return cached["releases"], cached["updated_at"]
            raise CatalogError(reason(e)) from e
        self.state.set_discography(source, artist_id, name, releases)
        return releases, time.time()


def request_row(rel: dict[str, Any], artist: str = "") -> dict[str, Any]:
    """What acquire.from_gap needs to know of who a release is by: the artist named,
    else its main artist (searches find a copy by name, so not "A feat. B")."""
    main = next((a["name"] for a in rel.get("artists") or [] if a["id"] != MB_VARIOUS), "")
    return {
        "source": rel["source"],
        "artist": artist or main or rel["artist"],
        "artist_original": "",
    }

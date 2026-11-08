"""Image sources for album covers and artist/circle pictures.

Each source returns ``ImageCandidate`` objects. Name-based sources only
return results whose artist (and album) names match after normalisation,
so a search for a small circle doesn't pick up an unrelated artist.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from .. import __version__

log = logging.getLogger("beets_ui.artwork")

USER_AGENT = f"beets-ui/{__version__} (+https://github.com/zcv28/beets-ui)"


@dataclass
class ImageCandidate:
    url: str
    source: str
    thumb: str = ""
    width: int | None = None
    height: int | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["thumb"] = self.thumb or self.url
        return d


@dataclass
class AlbumQuery:
    albumartist: str
    album: str
    mb_albumid: str = ""
    mb_releasegroupid: str = ""
    mb_albumartistid: str = ""
    catalognum: str = ""
    discogs_albumid: str = ""
    year: int = 0


@dataclass
class ArtistQuery:
    name: str
    mb_artistid: str = ""


def norm(s: str | None) -> str:
    """Normalise a name for comparison: NFKC, casefold, strip punctuation."""
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = re.sub(r"[\s\-_.,:;!?'\"()\[\]{}・／/&＆+]+", "", s)
    return s


def names_match(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    return bool(na) and na == nb


def contains_name(haystack: str, needle: str) -> bool:
    nn = norm(needle)
    return bool(nn) and nn in norm(haystack)


class Source:
    name = "base"
    label = "Base"
    albums = True
    artists = True
    min_interval = 0.25  # seconds between requests to the same source

    def __init__(self, client: httpx.Client, **keys: str):
        self.client = client
        self.keys = keys
        self._last = 0.0

    @property
    def available(self) -> bool:
        return True

    def get(self, url: str, **kw: Any) -> Any:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        r = self.client.get(url, **kw)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def album_covers(self, q: AlbumQuery) -> list[ImageCandidate]:
        return []

    def artist_images(self, q: ArtistQuery) -> list[ImageCandidate]:
        return []


class CoverArtArchive(Source):
    name, label, artists = "coverartarchive", "Cover Art Archive", False

    def album_covers(self, q: AlbumQuery) -> list[ImageCandidate]:
        out: list[ImageCandidate] = []
        for kind, mbid in (("release", q.mb_albumid), ("release-group", q.mb_releasegroupid)):
            if not mbid:
                continue
            data = self.get(f"https://coverartarchive.org/{kind}/{mbid}")
            for img in (data or {}).get("images", []):
                if not img.get("front") and "Front" not in (img.get("types") or []):
                    continue
                thumbs = img.get("thumbnails") or {}
                out.append(
                    ImageCandidate(
                        url=img["image"],
                        thumb=thumbs.get("500") or thumbs.get("large") or img["image"],
                        source=self.label,
                        note=kind,
                    )
                )
            if out:
                break
        return out


class FanartTv(Source):
    name, label = "fanarttv", "fanart.tv"

    @property
    def available(self) -> bool:
        return bool(self.keys.get("fanarttv"))

    def _get(self, path: str) -> Any:
        return self.get(
            f"https://webservice.fanart.tv/v3/music/{path}",
            params={"api_key": self.keys["fanarttv"]},
        )

    def album_covers(self, q: AlbumQuery) -> list[ImageCandidate]:
        if not q.mb_releasegroupid:
            return []
        data = self._get(f"albums/{q.mb_releasegroupid}") or {}
        out = []
        for album in (data.get("albums") or {}).values():
            for img in album.get("albumcover", []):
                out.append(
                    ImageCandidate(
                        url=img["url"],
                        thumb=img["url"].replace("/fanart/", "/preview/"),
                        source=self.label,
                        width=1000,
                        height=1000,
                    )
                )
        return out

    def artist_images(self, q: ArtistQuery) -> list[ImageCandidate]:
        if not q.mb_artistid:
            return []
        data = self._get(q.mb_artistid) or {}
        out = []
        for img in data.get("artistthumb", []):
            out.append(
                ImageCandidate(
                    url=img["url"],
                    thumb=img["url"].replace("/fanart/", "/preview/"),
                    source=self.label,
                    width=int(img.get("width") or 1000),
                    height=int(img.get("height") or 1000),
                )
            )
        return out


class TheAudioDB(Source):
    name, label = "theaudiodb", "TheAudioDB"
    min_interval = 1.0  # the free key is heavily rate-limited

    def _api(self, endpoint: str, **params: str) -> Any:
        key = self.keys.get("theaudiodb") or "2"
        return self.get(f"https://www.theaudiodb.com/api/v1/json/{key}/{endpoint}", params=params)

    def album_covers(self, q: AlbumQuery) -> list[ImageCandidate]:
        albums = []
        if q.mb_releasegroupid:
            albums = (self._api("album-mb.php", i=q.mb_releasegroupid) or {}).get("album") or []
        if not albums and q.albumartist and q.album:
            res = self._api("searchalbum.php", s=q.albumartist, a=q.album) or {}
            albums = [
                a
                for a in (res.get("album") or [])
                if names_match(a.get("strAlbum", ""), q.album)
                and names_match(a.get("strArtist", ""), q.albumartist)
            ]
        return [
            ImageCandidate(
                url=a["strAlbumThumb"], thumb=a["strAlbumThumb"] + "/preview", source=self.label
            )
            for a in albums
            if a.get("strAlbumThumb")
        ]

    def artist_images(self, q: ArtistQuery) -> list[ImageCandidate]:
        artists = []
        if q.mb_artistid:
            artists = (self._api("artist-mb.php", i=q.mb_artistid) or {}).get("artists") or []
        if not artists and q.name:
            res = self._api("search.php", s=q.name) or {}
            artists = [
                a for a in (res.get("artists") or []) if names_match(a.get("strArtist", ""), q.name)
            ]
        out = []
        for a in artists:
            for field in ("strArtistThumb", "strArtistWideThumb"):
                if a.get(field):
                    out.append(
                        ImageCandidate(
                            url=a[field],
                            thumb=a[field] + "/preview",
                            source=self.label,
                            note="wide" if "Wide" in field else "",
                        )
                    )
        return out


class Deezer(Source):
    name, label = "deezer", "Deezer"

    def album_covers(self, q: AlbumQuery) -> list[ImageCandidate]:
        if not (q.albumartist and q.album):
            return []
        res = self.get(
            "https://api.deezer.com/search/album",
            params={"q": f'artist:"{q.albumartist}" album:"{q.album}"', "limit": 10},
        )
        out = []
        for a in (res or {}).get("data", []):
            if (
                names_match(a.get("title", ""), q.album)
                and names_match((a.get("artist") or {}).get("name", ""), q.albumartist)
                and a.get("cover_xl")
            ):
                out.append(
                    ImageCandidate(
                        url=a["cover_xl"],
                        thumb=a.get("cover_medium", ""),
                        source=self.label,
                        width=1000,
                        height=1000,
                    )
                )
        return out

    def artist_images(self, q: ArtistQuery) -> list[ImageCandidate]:
        if not q.name:
            return []
        res = self.get("https://api.deezer.com/search/artist", params={"q": q.name, "limit": 10})
        out = []
        for a in (res or {}).get("data", []):
            url = a.get("picture_xl") or ""
            # Deezer serves a generic silhouette when there is no picture.
            if not url or "/artist//" in url:
                continue
            if names_match(a.get("name", ""), q.name):
                out.append(
                    ImageCandidate(
                        url=url,
                        thumb=a.get("picture_medium", ""),
                        source=self.label,
                        width=1000,
                        height=1000,
                    )
                )
        return out


class ITunes(Source):
    name, label, artists = "itunes", "iTunes", False

    def album_covers(self, q: AlbumQuery) -> list[ImageCandidate]:
        if not (q.albumartist and q.album):
            return []
        res = self.get(
            "https://itunes.apple.com/search",
            params={"term": f"{q.albumartist} {q.album}", "entity": "album", "limit": 10},
        )
        out = []
        for a in (res or {}).get("results", []):
            art = a.get("artworkUrl100") or ""
            if not art:
                continue
            if contains_name(a.get("collectionName", ""), q.album) and names_match(
                a.get("artistName", ""), q.albumartist
            ):
                out.append(
                    ImageCandidate(
                        url=art.replace("100x100bb", "1200x1200bb"),
                        thumb=art.replace("100x100bb", "300x300bb"),
                        source=self.label,
                        width=1200,
                        height=1200,
                        note=a.get("collectionName", ""),
                    )
                )
        return out


class Discogs(Source):
    name, label = "discogs", "Discogs"
    min_interval = 1.1  # 60 requests/minute

    @property
    def available(self) -> bool:
        return bool(self.keys.get("discogs"))

    def _get(self, path: str, **params: Any) -> Any:
        return self.get(
            f"https://api.discogs.com/{path}",
            params=params,
            headers={"Authorization": f"Discogs token={self.keys['discogs']}"},
        )

    @staticmethod
    def _images(data: dict, label: str) -> list[ImageCandidate]:
        imgs = sorted(data.get("images") or [], key=lambda i: i.get("type") != "primary")
        return [
            ImageCandidate(
                url=i["uri"],
                thumb=i.get("uri150", ""),
                source=label,
                width=i.get("width"),
                height=i.get("height"),
            )
            for i in imgs
            if i.get("uri")
        ]

    def album_covers(self, q: AlbumQuery) -> list[ImageCandidate]:
        if q.discogs_albumid:
            return self._images(self._get(f"releases/{q.discogs_albumid}") or {}, self.label)
        if not (q.albumartist and q.album):
            return []
        res = self._get(
            "database/search",
            artist=q.albumartist,
            release_title=q.album,
            type="release",
            per_page=5,
        )
        out = []
        for r in (res or {}).get("results", []):
            if r.get("cover_image") and contains_name(r.get("title", ""), q.album):
                out.append(
                    ImageCandidate(
                        url=r["cover_image"],
                        thumb=r.get("thumb", ""),
                        source=self.label,
                        note=r.get("title", ""),
                    )
                )
        return out

    def artist_images(self, q: ArtistQuery) -> list[ImageCandidate]:
        res = self._get("database/search", q=q.name, type="artist", per_page=5) or {}
        for r in res.get("results", []):
            if names_match(r.get("title", ""), q.name) and r.get("id"):
                return self._images(self._get(f"artists/{r['id']}") or {}, self.label)
        return []


class VocaDBFamily(Source):
    """VocaDB, UtaiteDB and TouhouDB share the same software and API."""

    base = "https://vocadb.net"

    def _names(self, entry: dict) -> list[str]:
        names = [entry.get("name", ""), entry.get("defaultName", "")]
        names += [n.get("value", "") for n in entry.get("names") or []]
        if entry.get("additionalNames"):
            names += [n.strip() for n in entry["additionalNames"].split(",")]
        return [n for n in names if n]

    @staticmethod
    def _pic(entry: dict) -> tuple[str, str]:
        pic = entry.get("mainPicture") or {}
        return pic.get("urlOriginal", ""), pic.get("urlThumb", "") or pic.get("urlSmallThumb", "")

    def album_covers(self, q: AlbumQuery) -> list[ImageCandidate]:
        results: list[dict] = []
        params = {"fields": "MainPicture,Names", "maxResults": 15, "nameMatchMode": "Auto"}
        if q.catalognum:
            res = self.get(f"{self.base}/api/albums", params={**params, "query": q.catalognum})
            results = [
                a
                for a in (res or {}).get("items", [])
                if norm(a.get("catalogNumber")) == norm(q.catalognum)
            ]
        if not results and q.album:
            res = self.get(f"{self.base}/api/albums", params={**params, "query": q.album})
            for a in (res or {}).get("items", []):
                if not any(names_match(n, q.album) for n in self._names(a)):
                    continue
                artist_ok = not q.albumartist or contains_name(
                    a.get("artistString", ""), q.albumartist
                )
                if artist_ok:
                    results.append(a)
        out = []
        for a in results:
            url, thumb = self._pic(a)
            if url:
                out.append(
                    ImageCandidate(
                        url=url, thumb=thumb, source=self.label, note=a.get("artistString", "")
                    )
                )
        return out

    def artist_images(self, q: ArtistQuery) -> list[ImageCandidate]:
        if not q.name:
            return []
        res = self.get(
            f"{self.base}/api/artists",
            params={
                "query": q.name,
                "fields": "MainPicture,Names",
                "nameMatchMode": "Exact",
                "maxResults": 10,
            },
        )
        out = []
        for a in (res or {}).get("items", []):
            if not any(names_match(n, q.name) for n in self._names(a)):
                continue
            url, thumb = self._pic(a)
            if url:
                out.append(
                    ImageCandidate(
                        url=url, thumb=thumb, source=self.label, note=a.get("artistType", "")
                    )
                )
        return out


class VocaDB(VocaDBFamily):
    name, label, base = "vocadb", "VocaDB", "https://vocadb.net"


class UtaiteDB(VocaDBFamily):
    name, label, base = "utaitedb", "UtaiteDB", "https://utaitedb.net"


class TouhouDB(VocaDBFamily):
    name, label, base = "touhoudb", "TouhouDB", "https://touhoudb.com"


SOURCES: dict[str, type[Source]] = {
    cls.name: cls
    for cls in (
        CoverArtArchive,
        FanartTv,
        TheAudioDB,
        Deezer,
        ITunes,
        Discogs,
        VocaDB,
        UtaiteDB,
        TouhouDB,
    )
}


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=20,
        follow_redirects=True,
        transport=transport,
    )

"""Small covers for releases the library doesn't have (Missing, Discover, Coming in,
Find, and the inbox folders a lookup matched to a release).

Fetched once from where the release was looked up (the Cover Art Archive for a
MusicBrainz release group or release, the album picture for VocaDB, TouhouDB and
UtaiteDB), kept on disk, and served from beets-ui so pages never link out. A
release with no cover is remembered for a week before it is asked again.

``full_url`` is the same release's cover at full size, for an album that was
imported without one.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from .discography import USER_AGENT, UUID, VOCADB

log = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024
MISS_KEEP = 7 * 86400
TYPES = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG": "image/png",
    b"GIF8": "image/gif",
    b"RIFF": "image/webp",
}
DIGITS = re.compile(r"^\d{1,12}$")
# A MusicBrainz release (what a lookup matches), next to "musicbrainz": a release group.
MB_RELEASE = "musicbrainz-release"
CAA = "https://coverartarchive.org"


def remote_url(source: str, release_id: str) -> str | None:
    """Where a release's small cover is, or None for an unknown source or ID."""
    if source in VOCADB and DIGITS.match(release_id):
        return f"{VOCADB[source]}/Album/CoverPictureThumb/{release_id}"
    if source == "musicbrainz" and UUID.match(release_id):
        return f"{CAA}/release-group/{release_id}/front-250"
    if source == MB_RELEASE and UUID.match(release_id):
        return f"{CAA}/release/{release_id}/front-250"
    return None


def candidate_ref(source: object, release_id: object) -> tuple[str, str] | None:
    """(source, id) as this module names them, for a lookup candidate's data source and
    ID ("MusicBrainz" and a release ID, "VocaDB" and an album number); None for a source
    whose covers can't be fetched by ID."""
    src, rid = str(source or "").lower(), str(release_id or "")
    if src == "musicbrainz":
        src = MB_RELEASE
    return (src, rid) if remote_url(src, rid) else None


def path_for(source: str, release_id: str) -> str | None:
    """The page's address for a release's cover, or None when it can't have one."""
    if remote_url(source, release_id) is None:
        return None
    return f"/get/cover/{source}/{release_id}"


def kind(data: bytes) -> str | None:
    return next((t for magic, t in TYPES.items() if data.startswith(magic)), None)


class ImageCache:
    """Small images kept on disk by key; a key without an image is remembered for a week."""

    def __init__(self, cache_dir: Path, http: httpx.Client | None = None):
        self.dir = cache_dir
        self._http = http

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                headers={"User-Agent": USER_AGENT}, timeout=10, follow_redirects=True
            )
        return self._http

    def _file(self, key: str) -> Path:
        h = hashlib.sha1(key.encode()).hexdigest()
        return self.dir / h[:2] / h

    def _cached(
        self,
        key: str,
        url: Callable[[], str],
        real: Callable[[httpx.Response], bool] = lambda r: True,
    ) -> tuple[Path, str] | None:
        """(file, media type) for ``key``, fetched from ``url()`` the first time.

        ``url()`` returns "" when there is no image (remembered), and raises
        ``httpx.HTTPError`` when it couldn't tell (asked again next time). ``real`` tells
        an image apart from a source's placeholder."""
        f = self._file(key)
        miss = f.with_suffix(".none")
        if f.exists():
            t = kind(f.read_bytes()[:8])
            if t:
                return f, t
        if miss.exists() and time.time() - miss.stat().st_mtime < MISS_KEEP:
            return None
        try:
            where = url()
            r = self.http.get(where) if where else None
        except (httpx.HTTPError, ValueError) as e:
            log.info("image for %s: %s", key, e)
            return None  # not remembered: the next page asks again
        f.parent.mkdir(parents=True, exist_ok=True)
        if r is None:
            miss.touch()
            return None
        data = r.content[: MAX_BYTES + 1]
        t = kind(data[:8]) if r.status_code == 200 and len(data) <= MAX_BYTES else None
        if t is not None and not real(r):
            t = None
        if t is None:
            if r.status_code in (200, 404):
                miss.touch()
            return None
        f.write_bytes(data)
        miss.unlink(missing_ok=True)
        return f, t


class ReleaseCovers(ImageCache):
    def full_url(self, source: str, release_id: str) -> str | None:
        """Where the release's cover is at full size, or None when it has none."""
        if remote_url(source, release_id) is None:
            return None
        if source in VOCADB:
            try:
                r = self.http.get(
                    f"{VOCADB[source]}/api/albums/{release_id}", params={"fields": "MainPicture"}
                )
                pic = (r.json() if r.status_code == 200 else {}).get("mainPicture") or {}
            except (httpx.HTTPError, ValueError) as e:
                log.info("cover for %s %s: %s", source, release_id, e)
                return None
            return pic.get("urlOriginal") or None
        kind = "release" if source == MB_RELEASE else "release-group"
        return f"{CAA}/{kind}/{release_id}/front"

    def get(self, source: str, release_id: str) -> tuple[Path, str] | None:
        """(file, media type) of the cover, fetching it the first time; None if there is none."""
        url = remote_url(source, release_id)
        if url is None:
            return None
        # A real VocaDB picture redirects to the site's image host; an album without one
        # gets the site's "?" placeholder straight away.
        real = (lambda r: bool(r.history)) if source in VOCADB else (lambda r: True)
        return self._cached(f"{source}:{release_id}", lambda: url, real)

"""Pictures of artists as the catalogues know them, for the Get artist page of someone
the library has no albums by (or no picture of).

Found by the artist's ID only, never by name, so a picture is never someone else's:
VocaDB, TouhouDB and UtaiteDB give the artist's own picture; for a MusicBrainz artist,
fanart.tv (with an API key) or Wikimedia Commons through the artist's MusicBrainz
links. Kept on disk like release covers, and an artist without one is asked again
after a week.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .discography import UUID, VOCADB
from .release_covers import DIGITS, ImageCache

# Artwork sources that find an artist by MusicBrainz ID.
BY_MBID = ["fanarttv", "wikidata"]


def known(source: str, artist_id: str) -> bool:
    if source in VOCADB:
        return bool(DIGITS.match(artist_id))
    return source == "musicbrainz" and bool(UUID.match(artist_id))


class ArtistPictures(ImageCache):
    def __init__(self, cache_dir: Path, artwork: Any, http: httpx.Client | None = None):
        super().__init__(cache_dir, http)
        self.artwork = artwork

    def remote_url(self, source: str, artist_id: str) -> str:
        """The picture's address, or "" when the artist has none. Raises
        ``httpx.HTTPError`` when a source couldn't be asked."""
        if source in VOCADB:
            r = self.http.get(
                f"{VOCADB[source]}/api/artists/{artist_id}", params={"fields": "MainPicture"}
            )
            if r.status_code == 404:
                return ""
            r.raise_for_status()
            pic = r.json().get("mainPicture") or {}
            return pic.get("urlThumb") or pic.get("urlOriginal") or ""
        failed: Exception | None = None
        for src in self.artwork.enabled("artist", BY_MBID):
            try:
                found = src.artist_images(_query(artist_id))
            except Exception as e:  # noqa: BLE001 - try the next source
                failed = e
                continue
            for c in found:
                if c.match == "id":
                    return c.thumb or c.url
        if failed is not None:
            raise httpx.HTTPError(str(failed))
        return ""

    def get(self, source: str, artist_id: str) -> tuple[Path, str] | None:
        """(file, media type) of the artist's picture; None if there is none (yet)."""
        if not known(source, artist_id):
            return None
        return self._cached(
            f"artist:{source}:{artist_id}", lambda: self.remote_url(source, artist_id)
        )


def _query(mbid: str) -> Any:
    from .artwork.sources import ArtistQuery

    return ArtistQuery(name="", mb_artistid=mbid)

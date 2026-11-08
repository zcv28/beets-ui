"""Album cover and artist image management on top of the beets library."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
from beets import config
from beets.dbcore.query import MatchQuery, PathQuery

from .. import beets_ctx
from ..jobs import JobContext, JobQueue
from ..settings import Settings
from .sources import (
    SOURCES,
    AlbumQuery,
    ArtistQuery,
    ImageCandidate,
    Source,
    make_client,
    strict_name,
)

log = logging.getLogger("beets_ui.artwork")

MAX_IMAGE_BYTES = 25 * 1024 * 1024


class ArtworkError(Exception):
    pass


def _write_error(path: Path | str, err: Exception) -> ArtworkError:
    """Explain a failed write, including which user the container runs as."""
    reason = getattr(err, "strerror", None) or str(err)
    who = f"uid {os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "this user"
    hint = ""
    if isinstance(err, PermissionError) or "ermission" in str(err):
        hint = f" - beets-ui runs as {who}; make the folder writable for it"
    return ArtworkError(f"cannot write {path}: {reason}{hint}")


def image_size(path: Path | str) -> tuple[int, int] | None:
    from PIL import Image

    try:
        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return None


def validate_image(data: bytes) -> tuple[str, tuple[int, int]]:
    """Return (extension, size) for image bytes, or raise ArtworkError."""
    from PIL import Image

    if len(data) > MAX_IMAGE_BYTES:
        raise ArtworkError("image too large")
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").lower()
            size = im.size
            im.verify()
    except Exception as e:  # noqa: BLE001
        raise ArtworkError(f"not a valid image: {e}") from e
    ext = {"jpeg": "jpg", "png": "png", "webp": "webp", "gif": "gif"}.get(fmt)
    if not ext:
        raise ArtworkError(f"unsupported image format {fmt or '?'}")
    return ext, size


def album_query(album: Any) -> AlbumQuery:
    return AlbumQuery(
        albumartist=album.albumartist or "",
        album=album.album or "",
        mb_albumid=album.mb_albumid or "",
        mb_releasegroupid=album.mb_releasegroupid or "",
        mb_albumartistid=(album.mb_albumartistid or "").split(";")[0].strip(),
        catalognum=album.catalognum or "",
        discogs_albumid=str(album.get("discogs_albumid") or "")
        if album.get("discogs_albumid")
        else "",
        year=album.year or 0,
    )


class Artwork:
    def __init__(
        self, settings: Settings, jobs: JobQueue, transport: httpx.BaseTransport | None = None
    ):
        self.settings = settings
        self.jobs = jobs
        self.state = jobs.state
        self._embedded_cache: dict[tuple[str, int], bool] = {}
        self.client = make_client(transport)
        keys = {
            "fanarttv": settings.fanarttv_api_key,
            "theaudiodb": settings.theaudiodb_api_key,
            "discogs": settings.discogs_token,
        }
        self.sources: dict[str, Source] = {
            n: cls(self.client, **keys) for n, cls in SOURCES.items()
        }
        self.thumb_dir = settings.data_dir / "thumbs"
        jobs.register("covers-fill")(self._job_fill_covers)
        jobs.register("artists-fill")(self._job_fill_artists)
        jobs.register("cover-set")(self._job_set_cover)
        jobs.register("artist-set")(self._job_set_artist)

    # ----------------------------------------------------------- sources

    def enabled(self, kind: str, override: list[str] | None = None) -> list[Source]:
        order = (
            override
            if override
            else (self.settings.cover_sources if kind == "album" else self.settings.artist_sources)
        )
        out = []
        for name in order:
            src = self.sources.get(name)
            if src is None or not src.available:
                continue
            if (kind == "album" and src.albums) or (kind == "artist" and src.artists):
                out.append(src)
        return out

    def available_sources(self, kind: str) -> list[dict[str, Any]]:
        configured = (
            self.settings.cover_sources if kind == "album" else self.settings.artist_sources
        )
        rows = []
        for name, src in self.sources.items():
            if (kind == "album" and not src.albums) or (kind == "artist" and not src.artists):
                continue
            rows.append(
                {
                    "name": name,
                    "label": src.label,
                    "available": src.available,
                    "enabled": name in configured,
                }
            )
        rows.sort(key=lambda r: configured.index(r["name"]) if r["name"] in configured else 99)
        return rows

    def _collect(self, sources: list[Source], fn: str, q: Any, first_only=False) -> list[dict]:
        found: list[ImageCandidate] = []
        errors = []
        for src in sources:
            try:
                res = getattr(src, fn)(q)
            except Exception as e:  # noqa: BLE001 - one bad source must not stop the rest
                log.info("%s lookup failed: %s", src.label, e)
                errors.append(f"{src.label}: {e}")
                continue
            found.extend(res)
            if first_only and res:
                break
        out = [c.as_dict() for c in found]
        if errors:
            out.append({"error": "; ".join(errors)})
        return out

    def find_album_covers(self, album: Any, sources: list[str] | None = None) -> list[dict]:
        return self._collect(self.enabled("album", sources), "album_covers", album_query(album))

    def find_artist_images(self, name: str, sources: list[str] | None = None) -> list[dict]:
        info = self.artist_info(name)
        q = ArtistQuery(name=name, mb_artistid=info["mbid"])
        return self._collect(self.enabled("artist", sources), "artist_images", q)

    # -------------------------------------------------------------- albums

    def cover_path(self, album: Any) -> Path | None:
        if album.artpath:
            p = Path(os.fsdecode(album.artpath))
            if p.is_file():
                return p
        return None

    def has_embedded(self, album: Any) -> bool:
        """Whether the album's first track carries embedded art (cached by mtime)."""
        from mediafile import MediaFile

        items = list(album.items())
        if not items:
            return False
        path = os.fsdecode(items[0].path)
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return False
        key = (path, mtime)
        if key not in self._embedded_cache:
            try:
                self._embedded_cache[key] = bool(MediaFile(path).images)
            except Exception:  # noqa: BLE001 - unreadable file: treat as no art
                self._embedded_cache[key] = False
        return self._embedded_cache[key]

    def cover_status(self, album: Any, check_embedded: bool = True) -> dict[str, Any]:
        """``has``: a cover file exists; ``embedded``: art is only inside the files."""
        p = self.cover_path(album)
        if p is None:
            embedded = check_embedded and self.has_embedded(album)
            return {"has": False, "embedded": embedded, "size": None, "low": not embedded}
        size = image_size(p)
        low = bool(size and min(size) < self.settings.min_cover_px)
        return {"has": True, "embedded": False, "size": size, "low": low}

    def thumbnail(self, src: Path, px: int = 300) -> Path:
        from PIL import Image

        st = src.stat()
        h = hashlib.sha1(f"{src}:{st.st_mtime_ns}:{st.st_size}:{px}".encode()).hexdigest()[:20]
        out = self.thumb_dir / f"{h}.jpg"
        if out.exists():
            return out
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((px, px))
            im.save(out, "JPEG", quality=82)
        return out

    def embedded_cover(self, album: Any) -> bytes | None:
        from mediafile import MediaFile

        for item in album.items():
            try:
                mf = MediaFile(os.fsdecode(item.path))
                if mf.art:
                    return mf.art
            except Exception:  # noqa: BLE001
                continue
            break
        return None

    def download(self, url: str) -> bytes:
        if not url.lower().startswith(("https://", "http://")):
            raise ArtworkError("only http(s) image URLs are supported")
        try:
            r = self.client.get(url, headers={"Accept": "image/*"})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ArtworkError(f"download failed: {e}") from e
        return r.content

    def save_album_cover(self, album: Any, data: bytes, embed: bool = False) -> Path:
        ext, _ = validate_image(data)
        fd, tmp = tempfile.mkstemp(suffix=f".{ext}", dir=self.settings.data_dir)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            album.set_art(os.fsencode(tmp), copy=True)
            album.store()
        except Exception as e:  # beets wraps OSError in FilesystemError
            dest = os.fsdecode(album.art_destination(os.fsencode(tmp)))
            raise _write_error(dest, getattr(e, "reason", None) or e) from e
        finally:
            Path(tmp).unlink(missing_ok=True)
        if embed:
            self.embed(album)
        return Path(os.fsdecode(album.artpath))

    def embed(self, album: Any) -> None:
        try:
            from beetsplug._utils.art import embed_album
        except ImportError:  # pragma: no cover - layout changed upstream
            log.warning("embedart helpers unavailable; run 'beet embedart' from the console")
            return
        try:
            maxwidth = int(config["embedart"]["maxwidth"].get() or 0) or None
        except Exception:  # noqa: BLE001 - option missing or not a number
            maxwidth = None
        embed_album(logging.getLogger("beets"), album, maxwidth=maxwidth, quiet=True)

    # ------------------------------------------------------------- artists

    def artists(self) -> list[dict[str, Any]]:
        lib = beets_ctx.lib()
        with lib.transaction() as tx:
            rows = tx.query(
                "SELECT albumartist, COUNT(*), MAX(mb_albumartistid) FROM albums "
                "WHERE albumartist != '' GROUP BY albumartist ORDER BY albumartist COLLATE NOCASE"
            )
        return [{"name": r[0], "albums": r[1], "mbid": (r[2] or "").split(";")[0]} for r in rows]

    def artist_info(self, name: str) -> dict[str, Any]:
        lib = beets_ctx.lib()
        albums = list(lib.albums(MatchQuery("albumartist", name)))
        mbid = next(
            (a.mb_albumartistid.split(";")[0].strip() for a in albums if a.mb_albumartistid), ""
        )
        folder, reason = self.artist_folder(name, albums)
        image = None
        if folder:
            for fn in self.settings.artist_image_names:
                if (folder / fn).is_file():
                    image = folder / fn
                    break
        return {
            "name": name,
            "albums": albums,
            "mbid": mbid,
            "folder": folder,
            "reason": reason,
            "image": image,
        }

    def artist_folder(self, name: str, albums: list[Any]) -> tuple[Path | None, str]:
        """Find the folder that holds only this album artist's albums."""
        if not albums:
            return None, "no albums"
        root = beets_ctx.library_dir().resolve()
        dirs = [Path(os.fsdecode(a.item_dir())).resolve() for a in albums if a.items()]
        if not dirs:
            return None, "no files"
        common = Path(os.path.commonpath([str(d) for d in dirs]))
        if common in dirs:
            # One album, or albums nested inside each other: go one level up.
            common = common.parent
        try:
            rel = common.relative_to(root)
        except ValueError:
            return None, "albums are outside the library directory"
        if str(rel) in ("", "."):
            return None, "albums are not grouped in a per-artist folder"
        lib = beets_ctx.lib()
        for item in lib.items(PathQuery("path", os.fsencode(str(common)))):
            if (item.albumartist or item.artist) != name:
                return None, f"folder {rel} also contains other artists"
        return common, ""

    def save_artist_image(self, name: str, data: bytes) -> list[Path]:
        ext, _ = validate_image(data)
        info = self.artist_info(name)
        folder = info["folder"]
        if folder is None:
            raise ArtworkError(f"cannot place an image for {name}: {info['reason']}")
        if ext != "jpg":
            data = _to_jpeg(data)
        written = []
        for fn in self.settings.artist_image_names:
            target = folder / fn
            tmp = target.with_suffix(target.suffix + ".tmp")
            try:
                tmp.write_bytes(data)
                tmp.replace(target)
            except OSError as e:
                tmp.unlink(missing_ok=True)
                raise _write_error(target, e) from e
            written.append(target)
        return written

    # ---------------------------------------------------------------- jobs

    def _job_set_cover(self, ctx: JobContext) -> None:
        album = beets_ctx.lib().get_album(int(ctx.params["album_id"]))
        if album is None:
            raise ArtworkError("album not found")
        data = self._params_image(ctx)
        path = self.save_album_cover(album, data, embed=bool(ctx.params.get("embed")))
        ctx.log(f"Saved cover for {album.albumartist} - {album.album} to {path}")

    def _job_set_artist(self, ctx: JobContext) -> None:
        name = ctx.params["artist"]
        data = self._params_image(ctx)
        for p in self.save_artist_image(name, data):
            ctx.log(f"Saved {p}")
        self.state.set_artist_art(
            name,
            "manual",
            source="upload" if ctx.params.get("upload") else "picked",
            url=ctx.params.get("url") or "",
        )

    def _params_image(self, ctx: JobContext) -> bytes:
        if ctx.params.get("upload"):
            p = Path(ctx.params["upload"])
            try:
                return p.read_bytes()
            finally:
                p.unlink(missing_ok=True)
        ctx.log(f"Downloading {ctx.params['url']}")
        return self.download(ctx.params["url"])

    def _job_fill_covers(self, ctx: JobContext) -> None:
        sources = ctx.params.get("sources") or None
        embed = bool(ctx.params.get("embed"))
        include_low = bool(ctx.params.get("low"))
        extract = ctx.params.get("extract", True)
        lib = beets_ctx.lib()
        done = extracted = failed = 0
        for album in lib.albums():
            st = self.cover_status(album)
            if st["has"] and not (include_low and st["low"]):
                continue
            label = f"{album.albumartist} - {album.album}"
            if st["embedded"]:
                if not extract:
                    continue
                data = self.embedded_cover(album)
                if data:
                    try:
                        self.save_album_cover(album, data)
                        ctx.log(f"✓ {label} (extracted embedded art)")
                        extracted += 1
                        continue
                    except ArtworkError as e:
                        ctx.log(f"  embedded art unusable: {e}")
            cands = [c for c in self.find_album_covers(album, sources) if "error" not in c]
            if include_low and st["size"]:
                cands = [c for c in cands if not c.get("width") or c["width"] > min(st["size"])]
            for c in cands:
                try:
                    self.save_album_cover(album, self.download(c["url"]), embed=embed)
                    ctx.log(f"✓ {label} ({c['source']})")
                    done += 1
                    break
                except ArtworkError as e:
                    ctx.log(f"  {c['source']}: {e}")
            else:
                ctx.log(f"✗ {label}: nothing found")
                failed += 1
        ctx.log(
            f"Done: {done} covers downloaded, {extracted} extracted from files, {failed} not found"
        )

    # ------------------------------------------------- artist auto-pick

    def pick_artist_image(
        self, name: str, mbid: str = "", sources: list[str] | None = None
    ) -> dict[str, Any]:
        """Choose an artist picture without asking, when that is safe.

        * A picture found through the MusicBrainz artist ID wins outright.
        * Otherwise the first source whose name search finds exactly one
          artist with that name is used (status "name": worth a glance).
        * A source that finds several different artists with the same name
          is ambiguous and is never used automatically.
        """
        q = ArtistQuery(name=name, mb_artistid=mbid)
        by_name: list[ImageCandidate] = []
        notes: list[str] = []
        for src in self.enabled("artist", sources):
            try:
                res = src.artist_images(q)
            except Exception as e:  # noqa: BLE001
                notes.append(f"{src.label}: error {e}")
                continue
            if not res:
                continue
            trusted = [c for c in res if c.match == "id"]
            if trusted:
                return {"status": "id", "candidates": trusted + by_name, "note": src.label}
            entities = {c.entity for c in res if c.entity}
            if len(entities) > 1:
                # Several artists match loosely (e.g. "bôa", "boa", "BoA"): use the
                # one spelled exactly like ours, if there is exactly one such.
                exact = [c for c in res if c.name and strict_name(c.name) == strict_name(name)]
                if len({c.entity for c in exact}) == 1:
                    by_name.append(exact[0])
                    continue
                notes.append(f"{src.label}: {len(entities)} different artists with this name")
                continue
            by_name.append(res[0])
        if by_name:
            return {"status": "name", "candidates": by_name, "note": by_name[0].source}
        if any("different artists" in n for n in notes):
            return {"status": "ambiguous", "candidates": [], "note": "; ".join(notes)}
        return {"status": "notfound", "candidates": [], "note": "; ".join(notes)}

    def _job_fill_artists(self, ctx: JobContext) -> None:
        sources = ctx.params.get("sources") or None
        overwrite = bool(ctx.params.get("overwrite", self.settings.artwork_overwrite))
        retry = bool(ctx.params.get("retry"))
        previous = self.state.artist_art()
        counts = {"id": 0, "name": 0, "ambiguous": 0, "notfound": 0, "nofolder": 0, "kept": 0}
        for a in self.artists():
            name = a["name"]
            info = self.artist_info(name)
            if info["folder"] is None:
                counts["nofolder"] += 1
                self.state.set_artist_art(name, "nofolder", note=info["reason"])
                continue
            if info["image"] and not overwrite:
                counts["kept"] += 1
                continue
            prev = previous.get(name, {}).get("status")
            if prev in ("ambiguous", "notfound") and not retry and not overwrite:
                counts[prev] += 1
                continue
            pick = self.pick_artist_image(name, info["mbid"], sources)
            status = pick["status"]
            saved = None
            for c in pick["candidates"]:
                try:
                    self.save_artist_image(name, self.download(c.url))
                    saved = c
                    break
                except ArtworkError as e:
                    ctx.log(f"  {name}: {c.source}: {e}")
            if pick["candidates"] and saved is None:
                status = "notfound"
            counts[status] += 1
            if saved is not None:
                how = "MusicBrainz ID" if status == "id" else "name match, check it"
                ctx.log(f"✓ {name}: {saved.source} ({how})")
                self.state.set_artist_art(name, status, source=saved.source, url=saved.url)
            else:
                ctx.log(f"✗ {name}: {status}{(' - ' + pick['note']) if pick['note'] else ''}")
                self.state.set_artist_art(name, status, note=pick["note"])
        ctx.log(
            "Done: {id} by MusicBrainz ID, {name} by name (review these), {ambiguous} ambiguous, "
            "{notfound} not found, {nofolder} without an artist folder, {kept} already had "
            "a picture".format(**counts)
        )


def _to_jpeg(data: bytes) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=92)
        return buf.getvalue()

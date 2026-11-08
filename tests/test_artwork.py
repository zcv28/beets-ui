from __future__ import annotations

import io

import httpx
import pytest
import respx

from beets_ui import beets_ctx, importer
from beets_ui.artwork.sources import (
    AlbumQuery,
    ArtistQuery,
    CoverArtArchive,
    Deezer,
    TouhouDB,
    make_client,
    names_match,
)

from conftest import make_album


def png_bytes(w=600, h=600) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


def test_names_match():
    assert names_match("IOSYS", "iosys")
    assert names_match("Sound Holic", "SOUND HOLIC")
    assert names_match("イオシス", "イオシス")
    assert not names_match("IOSYS", "IOSYS2")
    assert not names_match("", "")


@respx.mock
def test_touhoudb_artist_by_alias():
    respx.get("https://touhoudb.com/api/artists").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "name": "IOSYS",
                        "artistType": "Circle",
                        "names": [{"value": "イオシス"}, {"value": "IOSYS"}],
                        "mainPicture": {
                            "urlOriginal": "https://static/22.jpg",
                            "urlThumb": "https://static/t22.jpg",
                        },
                    },
                    {
                        "name": "Someone Else",
                        "names": [],
                        "mainPicture": {"urlOriginal": "https://static/99.jpg"},
                    },
                ]
            },
        )
    )
    src = TouhouDB(make_client())
    res = src.artist_images(ArtistQuery(name="イオシス"))
    assert [c.url for c in res] == ["https://static/22.jpg"]
    assert res[0].note == "Circle"


@respx.mock
def test_touhoudb_album_by_catalog():
    respx.get("https://touhoudb.com/api/albums").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "name": "X",
                        "catalogNumber": "IO-0001",
                        "artistString": "IOSYS",
                        "mainPicture": {"urlOriginal": "https://static/a.jpg"},
                    },
                    {
                        "name": "Y",
                        "catalogNumber": "IO-0002",
                        "artistString": "IOSYS",
                        "mainPicture": {"urlOriginal": "https://static/b.jpg"},
                    },
                ]
            },
        )
    )
    res = TouhouDB(make_client()).album_covers(
        AlbumQuery(albumartist="IOSYS", album="X", catalognum="io-0001")
    )
    assert [c.url for c in res] == ["https://static/a.jpg"]


@respx.mock
def test_caa_front_only():
    respx.get("https://coverartarchive.org/release/abc").mock(
        return_value=httpx.Response(
            200,
            json={
                "images": [
                    {"image": "https://caa/back.jpg", "front": False, "types": ["Back"]},
                    {
                        "image": "https://caa/front.jpg",
                        "front": True,
                        "thumbnails": {"500": "https://caa/f500.jpg"},
                    },
                ]
            },
        )
    )
    res = CoverArtArchive(make_client()).album_covers(AlbumQuery("A", "B", mb_albumid="abc"))
    assert [(c.url, c.thumb) for c in res] == [("https://caa/front.jpg", "https://caa/f500.jpg")]


@respx.mock
def test_deezer_skips_placeholder_and_mismatch():
    respx.get("https://api.deezer.com/search/artist").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "Foo",
                        "picture_xl": "https://e-cdns-images.dzcdn.net/images/artist//1000x1000.jpg",
                    },
                    {"name": "Foo Fighters", "picture_xl": "https://x/ff.jpg"},
                    {"name": "foo", "picture_xl": "https://x/foo.jpg"},
                ]
            },
        )
    )
    res = Deezer(make_client()).artist_images(ArtistQuery("Foo"))
    assert [c.url for c in res] == ["https://x/foo.jpg"]


@pytest.fixture
def artwork(env, tmp_path):
    from beets_ui.artwork.service import Artwork
    from beets_ui.jobs import JobQueue
    from beets_ui.settings import Settings
    from beets_ui.state import State

    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return Artwork(settings, JobQueue(State(":memory:")))


def _import(env, name, artist, album, fake_source):
    folder = make_album(env["inbox"] / name, artist=artist, album=album)
    key = importer.preview_folder(str(folder))[0]["key"]
    importer.apply_folder(str(folder), {key: {"action": "asis"}})


def test_artist_folder_and_save(env, fake_source, artwork):
    _import(env, "a", "Circle A", "First", fake_source)
    _import(env, "b", "Circle A", "Second", fake_source)
    _import(env, "c", "Circle B", "Only", fake_source)
    info = artwork.artist_info("Circle A")
    root = beets_ctx.library_dir().resolve()
    assert info["folder"] == root / "Circle A"
    info_b = artwork.artist_info("Circle B")
    assert info_b["folder"] == root / "Circle B"  # single album -> parent folder

    written = artwork.save_artist_image("Circle A", png_bytes())
    assert [p.name for p in written] == ["folder.jpg"]
    assert (root / "Circle A" / "folder.jpg").read_bytes()[:2] == b"\xff\xd8"  # converted to JPEG
    assert artwork.artist_info("Circle A")["image"] is not None


def test_flat_layout_has_no_artist_folder(env, fake_source, artwork, monkeypatch):
    from beets import config

    with beets_ctx.config_overrides({"paths": {"default": "$albumartist - $album/$track $title"}}):
        _import(env, "a", "Flat", "One", fake_source)
        _import(env, "b", "Flat", "Two", fake_source)
        _import(env, "c", "Other", "Three", fake_source)
    assert config["paths"]["default"].get() != "$albumartist - $album/$track $title"
    info = artwork.artist_info("Flat")
    assert info["folder"] is None
    assert "not grouped" in info["reason"] or "other artists" in info["reason"]


def test_save_album_cover(env, fake_source, artwork):
    _import(env, "a", "Artist", "Album", fake_source)
    album = beets_ctx.lib().albums()[0]
    assert artwork.cover_status(album)["has"] is False
    path = artwork.save_album_cover(album, png_bytes(300, 300))
    assert path.name == "cover.png"
    st = artwork.cover_status(beets_ctx.lib().get_album(album.id))
    assert st["has"] and st["size"] == (300, 300) and st["low"]


def test_invalid_image_rejected(artwork):
    from beets_ui.artwork.service import ArtworkError, validate_image

    with pytest.raises(ArtworkError):
        validate_image(b"<html>not an image</html>")

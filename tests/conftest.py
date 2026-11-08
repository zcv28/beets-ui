from __future__ import annotations

import os
from pathlib import Path

import pytest

# One silent MPEG-1 Layer III frame (128 kbps, 44.1 kHz, 417 bytes).
_FRAME = bytes.fromhex("fffb9064") + bytes(413)


def make_mp3(path: Path, **tags) -> Path:
    from mediafile import MediaFile

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_FRAME * 40)
    mf = MediaFile(str(path))
    for k, v in tags.items():
        setattr(mf, k, v)
    mf.save()
    return path


def make_album(folder: Path, artist="Test Artist", album="Test Album", n=3, year=2020):
    for i in range(1, n + 1):
        make_mp3(
            folder / f"{i:02d} Track {i}.mp3",
            title=f"Track {i}",
            artist=artist,
            albumartist=artist,
            album=album,
            track=i,
            tracktotal=n,
            year=year,
        )
    return folder


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fresh beets config + library + inbox, with no network metadata source."""
    from beets_ui import beets_ctx

    beetsdir = tmp_path / "config" / "beets"
    beetsdir.mkdir(parents=True)
    music = tmp_path / "music"
    music.mkdir()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (beetsdir / "config.yaml").write_text(
        f"directory: {music}\n"
        f"library: {beetsdir / 'library.db'}\n"
        "plugins: []\n"
        "threaded: no\n"
        "import:\n  copy: yes\n  write: yes\n  log: null\n"
    )
    monkeypatch.setenv("BEETSDIR", str(beetsdir))
    monkeypatch.setenv("BEETSUI_INBOX", str(inbox))
    monkeypatch.setenv("BEETSUI_DATA", str(tmp_path / "config" / "beets-ui"))
    monkeypatch.setenv("BEETSUI_PASSWORD", "secret")
    monkeypatch.setenv("BEETSUI_AUTO_PREVIEW", "false")

    from beets import config

    config.clear()
    config._materialized = False
    config.read()
    beets_ctx.reset()
    beets_ctx.init(beetsdir, load_plugins=False)
    yield {"tmp": tmp_path, "beetsdir": beetsdir, "music": music, "inbox": inbox}
    beets_ctx.reset()


@pytest.fixture
def fake_source(monkeypatch):
    """Replace metadata lookups with a deterministic fake album."""
    from beets import metadata_plugins
    from beets.autotag import AlbumInfo, TrackInfo

    def make_info(album_id="fake-1", album="Test Album", artist="Test Artist", n=3):
        tracks = [
            TrackInfo(
                title=f"Track {i}",
                track_id=f"{album_id}-t{i}",
                artist=artist,
                index=i,
                medium=1,
                medium_index=i,
                length=1.0,
                data_source="Fake",
            )
            for i in range(1, n + 1)
        ]
        return AlbumInfo(
            tracks=tracks,
            album=album,
            album_id=album_id,
            artist=artist,
            artist_id="fake-artist",
            year=2020,
            data_source="Fake",
            mediums=1,
        )

    albums = {"fake-1": make_info(), "fake-2": make_info("fake-2", album="Other Album")}

    def candidates(items, artist, album, va_likely, *a, **k):
        yield from albums.values()

    def albums_for_ids(ids, *a, **k):
        for i in ids:
            if i in albums:
                yield albums[i]

    monkeypatch.setattr(metadata_plugins, "candidates", candidates)
    monkeypatch.setattr(metadata_plugins, "albums_for_ids", albums_for_ids)
    monkeypatch.setattr(metadata_plugins, "get_penalty", lambda *_: 0.0)
    import beets.autotag.match as match

    monkeypatch.setattr(match, "metadata_plugins", metadata_plugins)
    return albums


def fs(p) -> str:
    return os.fsdecode(p)

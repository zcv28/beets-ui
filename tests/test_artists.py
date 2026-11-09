from __future__ import annotations

from fastapi.testclient import TestClient
from mediafile import MediaFile

from beets_ui import artists, beets_ctx, importer
from beets_ui.settings import Settings

from conftest import make_album

COLLAB = "ヌジャベス & Joe Claussell"


def test_primary_of():
    known = {"ヌジャベス": {"name": "ヌジャベス"}, "nujabes": {"name": "Nujabes"}}
    # Multi-valued artist list from the source wins.
    assert artists.primary_of(COLLAB, ["ヌジャベス", "Joe Claussell"], {}) == "ヌジャベス"
    # Otherwise split, but only onto an artist already in the library.
    assert artists.primary_of(COLLAB, [], known) == "ヌジャベス"
    assert artists.primary_of("nujabes feat. Shing02", None, known) == "Nujabes"
    assert artists.primary_of("Simon & Garfunkel", ["Simon & Garfunkel"], known) == ""
    assert artists.primary_of("ヌジャベス", ["ヌジャベス"], known) == ""


def _collab_info():
    from beets.autotag import AlbumInfo, TrackInfo

    tracks = [
        TrackInfo(
            title=f"Track {i}",
            track_id=f"collab-t{i}",
            artist=COLLAB if i == 1 else None,
            artists=["ヌジャベス", "Joe Claussell"] if i == 1 else None,
            index=i,
            medium=1,
            medium_index=i,
            length=1.0,
            data_source="Fake",
        )
        for i in range(1, 4)
    ]
    return AlbumInfo(
        tracks=tracks,
        album="Collab",
        album_id="collab",
        artist=COLLAB,
        artists=["ヌジャベス", "Joe Claussell"],
        artist_id="id-nujabes",
        artists_ids=["id-nujabes", "id-joe"],
        artist_sort="Nujabes & Claussell, Joe",
        artists_sort=["Nujabes", "Claussell, Joe"],
        year=2020,
        data_source="Fake",
        mediums=1,
    )


def _import_collab(env, primary: bool):
    folder = make_album(env["inbox"] / "collab", artist=COLLAB, album="Collab")
    key = importer.preview_folder(str(folder))[0]["key"]
    out = importer.apply_folder(
        str(folder),
        {key: {"action": "apply", "id": "collab", "source": "Fake"}},
        primary_artist=primary,
    )
    assert out[key].result == "imported"
    return out[key]


def test_import_keeps_primary_album_artist(env, fake_source):
    fake_source["collab"] = _collab_info()
    outcome = _import_collab(env, primary=True)
    assert "-> 'ヌジャベス'" in outcome.detail

    album = beets_ctx.lib().albums()[0]
    assert album.albumartist == "ヌジャベス"
    assert album.albumartists == ["ヌジャベス"]
    assert album.albumartist_sort == "Nujabes"
    assert album.mb_albumartistid == "id-nujabes"
    items = sorted(album.items(), key=lambda i: i.track)
    # Track artists keep the full credit, also on tracks without their own artist.
    assert {i.artist for i in items} == {COLLAB}
    assert all(i.albumartist == "ヌジャベス" for i in items)
    assert all("/ヌジャベス/Collab/" in i.filepath.as_posix() for i in items)
    assert MediaFile(str(items[0].filepath)).albumartist == "ヌジャベス"


def test_import_without_option_keeps_full_credit(env, fake_source):
    fake_source["collab"] = _collab_info()
    _import_collab(env, primary=False)
    assert beets_ctx.lib().albums()[0].albumartist == COLLAB


def test_library_tool_merges_existing_albums(env, fake_source, monkeypatch):
    from beets_ui.main import create_app

    lib = beets_ctx.lib()
    for name, album in (("ヌジャベス", "Solo"), (COLLAB, "Collab"), ("Simon & Garfunkel", "Duo")):
        folder = make_album(env["inbox"] / album, artist=name, album=album)
        key = importer.preview_folder(str(folder))[0]["key"]
        importer.apply_folder(str(folder), {key: {"action": "asis"}})
    old_dir = env["music"] / COLLAB
    (old_dir / "folder.jpg").write_bytes(b"x")

    assert [(r["current"], r["primary"]) for r in artists.proposals(lib)] == [
        (COLLAB, "ヌジャベス")
    ]

    app = create_app(Settings.from_env(), background=False)
    c = TestClient(app)
    c.post("/login", data={"password": "secret"})
    page = c.get("/library/artists/primary")
    assert page.status_code == 200 and "ヌジャベス &amp; Joe Claussell" in page.text
    collab_id = next(a.id for a in lib.albums() if a.album == "Collab")
    r = c.post(
        "/library/artists/primary",
        data={"album": [str(collab_id)], "write": "1", "move": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    app.state.jobs.run_pending_sync()

    album = lib.get_album(collab_id)
    assert album.albumartist == "ヌジャベス"
    item = next(iter(album.items()))
    assert item.artist == COLLAB
    assert item.filepath.parent == env["music"] / "ヌジャベス" / "Collab"
    assert MediaFile(str(item.filepath)).albumartist == "ヌジャベス"
    assert not old_dir.exists()
    assert {a.albumartist for a in lib.albums()} == {"ヌジャベス", "Simon & Garfunkel"}
    assert artists.proposals(lib) == []

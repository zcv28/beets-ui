"""Auto-import, artist aliases, undo, and the small web additions."""

from __future__ import annotations

import copy
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from mediafile import MediaFile

from beets_ui import artists, beets_ctx, importer
from beets_ui.inbox import auto_import_blocker
from beets_ui.settings import Settings

from conftest import make_album


@pytest.fixture
def app(env, fake_source):
    from beets_ui.main import create_app

    return create_app(Settings.from_env(), background=False)


@pytest.fixture
def client(app):
    c = TestClient(app)
    c.post("/login", data={"password": "secret"})
    return c


def run_jobs(app):
    app.state.jobs.run_pending_sync()


def _asis(env, artist, album, **kw):
    folder = make_album(env["inbox"] / f"{artist}-{album}", artist=artist, album=album, **kw)
    key = importer.preview_folder(str(folder))[0]["key"]
    importer.apply_folder(str(folder), {key: {"action": "asis"}})
    return next(a for a in beets_ctx.lib().albums() if a.album == album)


# ----------------------------------------------------------------- auto-import


def _task(match=99.0, **cand):
    return {
        "cur_artist": "A",
        "cur_album": "B",
        "candidates": [{"match": match, "extra_tracks": [], "extra_items": [], **cand}],
    }


def test_auto_import_blocker():
    assert auto_import_blocker([_task()], [], 95) == ""
    assert "94.0%" in auto_import_blocker([_task(94.0)], [], 95)
    assert "missing 1" in auto_import_blocker([_task(extra_tracks=["x"])], [], 95)
    assert "match no track" in auto_import_blocker([_task(extra_items=["x.mp3"])], [], 95)
    assert "already in the library" in auto_import_blocker([_task()], [{"id": 1}], 95)
    assert "no candidates" in auto_import_blocker([{**_task(), "candidates": []}], [], 95)


@pytest.fixture
def auto_app(env, fake_source, monkeypatch):
    from beets_ui.main import create_app

    monkeypatch.setenv("BEETSUI_AUTO_IMPORT", "90")
    return create_app(Settings.from_env(), background=False)


def test_auto_import_after_automatic_lookup(auto_app, env, monkeypatch):
    app = auto_app
    monkeypatch.setattr(app.state.settings, "auto_preview", True)
    monkeypatch.setattr(app.state.settings, "debounce", 0)
    make_album(env["inbox"] / "auto")
    app.state.inbox.scan()  # queues the automatic lookup
    run_jobs(app)  # lookup, then the import it queued
    assert app.state.state.folder("auto")["status"] == "imported"
    assert [a.album for a in beets_ctx.lib().albums()] == ["Test Album"]
    assert app.state.state.jobs()[0]["user"] == "auto-import"


def test_manual_lookup_never_auto_imports(auto_app, env):
    client = TestClient(auto_app)
    client.post("/login", data={"password": "secret"})
    make_album(env["inbox"] / "manual")
    client.post("/inbox/scan")
    client.post("/inbox/folder/preview", data={"f": "manual"})
    run_jobs(auto_app)
    assert auto_app.state.state.folder("manual")["status"] == "ready"
    assert len(beets_ctx.lib().albums()) == 0


def test_auto_import_off_by_default(app, env):
    assert app.state.settings.auto_import == 0
    make_album(env["inbox"] / "x")
    app.state.inbox.scan()
    app.state.inbox.queue_preview("x", auto=True)
    run_jobs(app)
    assert app.state.state.folder("x")["status"] == "ready"


# --------------------------------------------------------------------- aliases


def test_alias_suggestions(env):
    lib = beets_ctx.lib()
    _asis(env, "Nujabes", "One")
    _asis(env, "Nujabes", "Two")
    _asis(env, "ヌジャベス", "Three")
    _asis(env, "ＮＵＪＡＢＥＳ", "Four")
    _asis(env, "Shing02 (シンゴツー)", "Five")
    _asis(env, "シンゴツー", "Six")
    _asis(env, "Simon & Garfunkel", "Seven")
    jp = next(a for a in lib.albums() if a.album == "Three")
    jp.albumartist_sort = "Nujabes"
    jp.store()

    groups = {g["target"]: [n["name"] for n in g["names"]] for g in artists.alias_suggestions(lib)}
    assert groups == {
        "Nujabes": ["Nujabes", "ヌジャベス", "ＮＵＪＡＢＥＳ"],
        "シンゴツー": ["シンゴツー", "Shing02 (シンゴツー)"],
    }


def test_alias_suggestions_need_same_source_for_ids(env):
    lib = beets_ctx.lib()
    for name, source in (("Circle A", "VocaDB"), ("Circle B", "TouhouDB"), ("Circle C", "VocaDB")):
        a = _asis(env, name, name + " album")
        a.mb_albumartistid = "123"
        a.data_source = source
        a.store()
    groups = artists.alias_suggestions(lib)
    assert [[n["name"] for n in g["names"]] for g in groups] == [["Circle A", "Circle C"]]


def test_merge_aliases_then_undo(app, client, env, fake_source):
    lib = beets_ctx.lib()
    _asis(env, "Nujabes", "One")
    jp = _asis(env, "ヌジャベス", "Two")
    jp.albumartist_sort = "Nujabes"  # as MusicBrainz tags it
    jp.store()
    (env["music"] / "ヌジャベス" / "folder.jpg").write_bytes(b"x")

    page = client.get("/library/artists/aliases")
    assert page.status_code == 200 and "is the sort or credited name of" in page.text
    r = client.post(
        "/library/artists/merge",
        data={
            "name": ["Nujabes", "ヌジャベス"],
            "target": "Nujabes",
            "write": "1",
            "move": "1",
            "tracks": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    merge_job = int(r.headers["location"].rsplit("/", 1)[1])
    run_jobs(app)
    assert app.state.state.job(merge_job)["status"] == "done"

    album = lib.get_album(jp.id)
    item = next(iter(album.items()))
    assert album.albumartist == "Nujabes" and item.artist == "Nujabes"
    assert item.filepath.parent == env["music"] / "Nujabes" / "Two"
    assert MediaFile(str(item.filepath)).albumartist == "Nujabes"
    assert not (env["music"] / "ヌジャベス").exists()
    assert app.state.state.artist_aliases() == {"ヌジャベス": "Nujabes"}

    # Future imports credited to the alias land under the kept name.
    info = fake_source["jp"] = copy.deepcopy(fake_source["fake-1"])
    info.album_id, info.album, info.artist = "jp", "Three", "ヌジャベス"
    for t in info.tracks:
        t.artist = "ヌジャベス"
        t.track_id = "jp-" + t.track_id
    folder = make_album(env["inbox"] / "three", artist="ヌジャベス", album="Three")
    key = importer.preview_folder(str(folder))[0]["key"]
    out = importer.apply_folder(
        str(folder),
        {key: {"action": "apply", "id": "jp", "source": "Fake"}},
        aliases=app.state.state.artist_aliases(),
    )
    assert "-> 'Nujabes'" in out[key].detail
    three = next(a for a in lib.albums() if a.album == "Three")
    assert three.albumartist == "Nujabes"
    assert {i.artist for i in three.items()} == {"Nujabes"}

    # Undo puts the old names, tags and folders back and forgets the alias.
    page = client.get(f"/jobs/{merge_job}")
    assert "Undo" in page.text
    r = client.post(f"/jobs/{merge_job}/undo", follow_redirects=False)
    undo_job = int(r.headers["location"].rsplit("/", 1)[1])
    run_jobs(app)
    assert app.state.state.job(undo_job)["status"] == "done", app.state.state.job(undo_job)["log"]
    album = lib.get_album(jp.id)
    item = next(iter(album.items()))
    assert album.albumartist == "ヌジャベス" and item.artist == "ヌジャベス"
    assert item.filepath.parent == env["music"] / "ヌジャベス" / "Two"
    assert MediaFile(str(item.filepath)).artist == "ヌジャベス"
    assert app.state.state.artist_aliases() == {}
    assert "Undone by" in client.get(f"/jobs/{merge_job}").text
    # A second undo is refused.
    client.post(f"/jobs/{merge_job}/undo")
    assert app.state.jobs.pending() == 0


def test_artists_page_letters_and_merge_bar(client, env):
    _asis(env, "Nujabes", "One")
    _asis(env, "ヌジャベス", "Two")
    _asis(env, "2814", "Three")
    page = client.get("/library/artists")
    assert page.status_code == 200
    assert 'data-letter="N"' in page.text and 'data-letter="あ"' in page.text
    assert 'data-letter="0–9"' in page.text
    assert 'id="merge-bar"' in page.text


# ------------------------------------------------------------------------ undo


def test_album_edit_undo(app, client, env):
    a = _asis(env, "Artist", "Album", year=2001)
    r = client.post(
        f"/library/album/{a.id}/edit",
        data={"year": "1999", "extra": "mood=calm", "write": "1"},
    )
    assert r.status_code == 200
    run_jobs(app)
    edit_job = app.state.state.jobs()[0]
    a = beets_ctx.lib().get_album(a.id)
    assert a.year == 1999 and a["mood"] == "calm"
    path = next(iter(a.items())).filepath
    assert MediaFile(str(path)).year == 1999

    client.post(f"/jobs/{edit_job['id']}/undo")
    run_jobs(app)
    a = beets_ctx.lib().get_album(a.id)
    assert a.year == 2001 and "mood" not in a
    assert {i.year for i in a.items()} == {2001}
    assert MediaFile(str(path)).year == 2001


def test_jobs_without_snapshot_have_no_undo(app, client):
    r = client.post("/console", data={"cmd": "version"}, follow_redirects=False)
    job_id = int(r.headers["location"].rsplit("/", 1)[1])
    run_jobs(app)
    assert "/undo" not in client.get(f"/jobs/{job_id}").text
    client.post(f"/jobs/{job_id}/undo")
    assert app.state.jobs.pending() == 0


# ------------------------------------------------------------------------- web


def test_inbox_audio_file(client, env):
    make_album(env["inbox"] / "f")
    (env["inbox"] / "f" / "notes.txt").write_text("x")
    (env["tmp"] / "secret.mp3").write_bytes(b"x")
    r = client.get("/inbox/file?p=" + quote("f/01 Track 1.mp3"))
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    r = client.get("/inbox/file?p=" + quote("f/01 Track 1.mp3"), headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and len(r.content) == 10
    absolute = str(env["inbox"] / "f" / "02 Track 2.mp3")
    assert client.get("/inbox/file?p=" + quote(absolute)).status_code == 200
    assert client.get("/inbox/file?p=" + quote("f/notes.txt")).status_code == 404
    assert client.get("/inbox/file?p=" + quote("../secret.mp3")).status_code == 404
    assert client.get("/inbox/file?p=" + quote(str(env["tmp"] / "secret.mp3"))).status_code == 404


def test_review_page_has_player_and_shortcuts(app, client, env):
    make_album(env["inbox"] / "f")
    client.post("/inbox/scan")
    client.post("/inbox/folder/preview", data={"f": "f"})
    run_jobs(app)
    page = client.get("/inbox/folder?f=f").text
    assert 'class="play"' in page and 'id="player"' in page
    assert 'class="sticky-bar"' in page and 'id="decided"' in page


def test_jobs_pill_reports_finished_jobs(app, client):
    since = time.time() - 1
    r = client.get(f"/jobs/pill?since={since}")
    assert "toast" not in r.text
    client.post("/console", data={"cmd": "version"})
    run_jobs(app)
    r = client.get(f"/jobs/pill?since={since}")
    assert 'class="toast done"' in r.text and "beet version" in r.text
    assert 'hx-swap-oob="beforeend:#toasts"' in r.text
    # Lookups only pop up when they fail.
    quiet = app.state.state.create_job("preview", "Look up x", {})
    app.state.state.set_job(quiet, status="done", finished_at=time.time())
    assert "Look up" not in client.get(f"/jobs/pill?since={since}").text


def test_album_action_buttons_queue_console_jobs(app, client, env):
    a = _asis(env, "Artist", "Album")
    r = client.post(
        f"/library/album/{a.id}/run", data={"action": "fetchart"}, follow_redirects=False
    )
    assert r.status_code == 303
    job = app.state.state.jobs()[0]
    assert job["kind"] == "console" and job["title"] == f"beet fetchart -f album_id:{a.id}"
    assert client.post(f"/library/album/{a.id}/run", data={"action": "rm"}).status_code == 400


def test_library_next_page_is_a_fragment(client, env, monkeypatch):
    from beets_ui import library

    for n in range(3):
        _asis(env, "Artist", f"Album {n}")
    real = library.albums
    monkeypatch.setattr(library, "albums", lambda q, s, page: real(q, s, page, per_page=2))
    first = client.get("/library").text
    assert "Album 0" in first and "Album 2" not in first and 'hx-trigger="revealed"' in first
    more = client.get("/library?page=2", headers={"HX-Request": "true"}).text
    assert "Album 2" in more and "<html" not in more and "revealed" not in more
    rows = client.get("/library?page=2&view=table", headers={"HX-Request": "true"}).text
    assert "<tr>" in rows and "Album 2" in rows
    tracks = client.get("/library/tracks?page=2", headers={"HX-Request": "true"}).text
    assert "<html" not in tracks

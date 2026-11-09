"""Regression tests for: thread path resolution, embedded art, inbox library
matches and bulk delete, artist auto-pick, Wikimedia source, accent folding."""

from __future__ import annotations

import io
import os
import threading
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from beets_ui import beets_ctx, importer
from beets_ui.artwork.sources import (
    ArtistQuery,
    ImageCandidate,
    Source,
    Wikimedia,
    make_client,
    names_match,
)
from beets_ui.settings import Settings

from conftest import make_album


def png(w=600, h=600) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


def import_asis(env, name, artist="Artist", album="Album"):
    folder = make_album(env["inbox"] / name, artist=artist, album=album)
    key = importer.preview_folder(str(folder))[0]["key"]
    importer.apply_folder(str(folder), {key: {"action": "asis"}})
    return folder


@pytest.fixture
def artwork(env):
    from beets_ui.artwork.service import Artwork
    from beets_ui.jobs import JobQueue
    from beets_ui.state import State

    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return Artwork(settings, JobQueue(State(":memory:")))


# --------------------------------------------------------- relative paths


def test_paths_resolve_in_other_threads(env, fake_source, artwork):
    """beets stores paths relative to the library; worker threads must expand them."""
    import_asis(env, "a")
    album = beets_ctx.lib().albums()[0]
    artwork.save_album_cover(album, png())
    album_id = album.id

    with beets_ctx.lib().transaction() as tx:
        stored = tx.query("SELECT artpath FROM albums WHERE id = ?", (album_id,))[0][0]
    assert not os.path.isabs(os.fsdecode(stored)), "expected beets to store a relative path"

    result = {}

    def worker():
        a = beets_ctx.lib().get_album(album_id)
        result["artpath"] = os.fsdecode(a.artpath)
        result["status"] = artwork.cover_status(a)
        result["item"] = os.fsdecode(a.items()[0].path)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert os.path.isabs(result["artpath"]) and os.path.isfile(result["artpath"])
    assert result["status"]["has"] is True
    assert os.path.isfile(result["item"])


# ----------------------------------------------------------- embedded art


def test_embedded_only_cover_and_extract(env, fake_source, artwork):
    from mediafile import Image, MediaFile

    folder = make_album(env["inbox"] / "emb", artist="Emb", album="Edded")
    for f in folder.iterdir():
        mf = MediaFile(str(f))
        mf.images = [Image(data=png(700, 700))]
        mf.save()
    key = importer.preview_folder(str(folder))[0]["key"]
    importer.apply_folder(str(folder), {key: {"action": "asis"}})
    album = beets_ctx.lib().albums()[0]

    st = artwork.cover_status(album)
    assert st == {"has": False, "embedded": True, "size": (700, 700), "low": False}

    from beets_ui.jobs import JobContext

    job_id = artwork.state.create_job("covers-fill", "t", {})
    artwork._job_fill_covers(JobContext(artwork.state, job_id, {"extract": True}))
    st = artwork.cover_status(beets_ctx.lib().get_album(album.id))
    assert st["has"] and st["size"] == (700, 700)


# ------------------------------------------------------------------ inbox


@pytest.fixture
def app(env, fake_source):
    from beets_ui.main import create_app

    return create_app(Settings.from_env(), background=False)


@pytest.fixture
def client(app):
    c = TestClient(app)
    c.post("/login", data={"password": "secret"})
    return c


def test_inbox_lists_files_and_detects_library_albums(app, client, env):
    # Import one copy, then drop a second copy of the same album in the inbox.
    make_album(env["inbox"] / "first", album="Same Album")
    client.post("/inbox/scan")
    client.post("/inbox/folder/preview", data={"f": "first"})
    app.state.jobs.run_pending_sync()
    client.post("/inbox/folder/import", data={"f": "first", "choice-0": "asis"})
    app.state.jobs.run_pending_sync()

    make_album(env["inbox"] / "again", album="Same Album")
    (env["inbox"] / "again" / "cover.jpg").write_bytes(b"x")
    make_album(env["inbox"] / "fresh", album="Brand New")
    client.post("/inbox/scan")

    again = app.state.state.folder("again")
    assert [f["path"] for f in again["files"] if not f["audio"]] == ["cover.jpg"]
    assert len([f for f in again["files"] if f["audio"]]) == 3
    assert again["library"] and again["library"][0]["album"] == "Same Album"
    assert app.state.state.folder("fresh")["library"] == []

    page = client.get("/inbox")
    assert "In Library (2)" in page.text  # imported "first" + "again"
    assert "01 Track 1.mp3" in page.text  # file listing dropdown
    assert "Delete Imported (2)" in page.text

    client.post("/inbox/delete-imported")
    assert not (env["inbox"] / "again").exists()
    assert not (env["inbox"] / "first").exists()
    assert (env["inbox"] / "fresh").exists()


def test_row_delete_returns_to_list(app, client, env):
    make_album(env["inbox"] / "x")
    client.post("/inbox/scan")
    app.state.state.upsert_folder("x", status="imported")
    r = client.post(
        "/inbox/folder/delete",
        data={"f": "x", "back": "/inbox?status=inlib"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/inbox?status=inlib"
    assert not (env["inbox"] / "x").exists()


def test_existing_state_db_gets_new_columns(tmp_path):
    import sqlite3

    from beets_ui.state import State

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE folders (path TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'new', "
        "signature TEXT NOT NULL DEFAULT '', changed_at REAL NOT NULL DEFAULT 0, "
        "updated_at REAL NOT NULL DEFAULT 0, best REAL, mode TEXT NOT NULL DEFAULT 'albums', "
        "error TEXT, tasks TEXT NOT NULL DEFAULT '[]', duplicates TEXT NOT NULL DEFAULT '{}', "
        "decisions TEXT NOT NULL DEFAULT '{}', job_id INTEGER)"
    )
    conn.execute("INSERT INTO folders (path) VALUES ('old')")
    conn.commit()
    conn.close()
    row = State(db).folder("old")
    assert row["files"] == [] and row["library"] == [] and row["tagkeys"] == {}


# ------------------------------------------------------- artist auto-pick


class FakeSource(Source):
    def __init__(self, name, results):
        super().__init__(make_client())
        self.name = self.label = name
        self.results = results

    def artist_images(self, q):
        return self.results


def _with_sources(artwork, *sources):
    artwork.sources = {s.name: s for s in sources}
    artwork.settings.artist_sources = [s.name for s in sources]


def test_pick_prefers_musicbrainz_id(artwork):
    _with_sources(
        artwork,
        FakeSource("byname", [ImageCandidate("https://n/1.jpg", "byname", entity="1")]),
        FakeSource("byid", [ImageCandidate("https://i/1.jpg", "byid", match="id")]),
    )
    pick = artwork.pick_artist_image("X", "mbid")
    assert pick["status"] == "id"
    assert pick["candidates"][0].url == "https://i/1.jpg"


def test_pick_by_name_and_ambiguity(artwork):
    ambiguous = FakeSource(
        "amb",
        [
            ImageCandidate("https://a/1.jpg", "amb", entity="1"),
            ImageCandidate("https://a/2.jpg", "amb", entity="2"),
        ],
    )
    single = FakeSource("one", [ImageCandidate("https://o/1.jpg", "one", entity="9")])
    _with_sources(artwork, ambiguous, single)
    pick = artwork.pick_artist_image("since")
    assert pick["status"] == "name" and pick["candidates"][0].url == "https://o/1.jpg"

    _with_sources(artwork, ambiguous)
    assert artwork.pick_artist_image("since")["status"] == "ambiguous"
    _with_sources(artwork, FakeSource("none", []))
    assert artwork.pick_artist_image("since")["status"] == "notfound"


def test_fill_job_records_results_and_skips_known_misses(env, fake_source, artwork, monkeypatch):
    from beets_ui.jobs import JobContext

    import_asis(env, "a", artist="Found", album="One")
    import_asis(env, "b", artist="Missing", album="Two")
    src = FakeSource("src", [])
    _with_sources(artwork, src)

    def images(q):
        if q.name == "Found":
            return [ImageCandidate("https://img/found.jpg", "src", entity="1")]
        return []

    src.artist_images = images
    monkeypatch.setattr(artwork, "download", lambda url: png())

    def run(**params):
        job_id = artwork.state.create_job("artists-fill", "t", params)
        ctx = JobContext(artwork.state, job_id, params)
        artwork._job_fill_artists(ctx)
        ctx.flush()
        return artwork.state.job(job_id)["log"]

    log = run()
    res = artwork.state.artist_art()
    assert res["Found"]["status"] == "name" and res["Missing"]["status"] == "notfound"
    assert artwork.artist_info("Found")["image"] is not None
    assert "1 by name" in log

    calls = []
    src.artist_images = lambda q: calls.append(q.name) or []
    run()
    assert calls == []  # "Missing" is skipped until retry; "Found" already has a picture
    run(retry=True)
    assert calls == ["Missing"]


# --------------------------------------------------------------- sources


@respx.mock
def test_wikimedia_via_musicbrainz_wikidata():
    respx.get("https://musicbrainz.org/ws/2/artist/abc").mock(
        return_value=httpx.Response(
            200,
            json={
                "relations": [
                    {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q42"}}
                ]
            },
        )
    )
    respx.get("https://www.wikidata.org/wiki/Special:EntityData/Q42.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "entities": {
                    "Q42": {
                        "claims": {
                            "P18": [{"mainsnak": {"datavalue": {"value": "Casiopea live.jpg"}}}]
                        }
                    }
                }
            },
        )
    )
    src = Wikimedia(make_client())
    src.min_interval = 0
    res = src.artist_images(ArtistQuery("Casiopea", mb_artistid="abc"))
    assert len(res) == 1 and res[0].match == "id"
    assert res[0].url.endswith("Special:FilePath/Casiopea_live.jpg?width=1500")
    assert src.artist_images(ArtistQuery("No MBID")) == []


@respx.mock
def test_wikimedia_prefers_image_relation():
    respx.get("https://musicbrainz.org/ws/2/artist/abc").mock(
        return_value=httpx.Response(
            200,
            json={
                "relations": [
                    {
                        "type": "image",
                        "url": {"resource": "https://commons.wikimedia.org/wiki/File:B%C3%B4a.jpg"},
                    }
                ]
            },
        )
    )
    src = Wikimedia(make_client())
    src.min_interval = 0
    res = src.artist_images(ArtistQuery("bôa", mb_artistid="abc"))
    assert "B%C3%B4a.jpg" in res[0].url


def test_accent_folding_only_for_latin():
    assert names_match("bôa", "boa")
    assert names_match("Beyoncé", "BEYONCE")
    assert names_match("chouchou merged syrups.", "Chouchou Merged Syrups")
    assert not names_match("ガ", "カ")


def test_pick_resolves_loose_matches_by_exact_spelling(artwork):
    loose = FakeSource(
        "deezer",
        [
            ImageCandidate("https://d/boa.jpg", "deezer", entity="1", name="boa"),
            ImageCandidate("https://d/bôa.jpg", "deezer", entity="2", name="bôa"),
            ImageCandidate("https://d/BoA.jpg", "deezer", entity="3", name="BoA"),
        ],
    )
    _with_sources(artwork, loose)
    pick = artwork.pick_artist_image("bôa")
    assert pick["status"] == "name" and pick["candidates"][0].url == "https://d/bôa.jpg"
    # "boa" is spelled exactly by two different artists here -> still ambiguous
    assert artwork.pick_artist_image("boa")["status"] == "ambiguous"


def test_unwritable_artist_folder_is_reported_not_fatal(env, fake_source, artwork, monkeypatch):
    from beets_ui.artwork.service import ArtworkError
    from beets_ui.jobs import JobContext

    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    import_asis(env, "a", artist="ヨルシカ", album="One")
    import_asis(env, "b", artist="Writable", album="Two")
    locked = beets_ctx.library_dir() / "ヨルシカ"
    locked.chmod(0o555)
    try:
        with pytest.raises(ArtworkError, match=r"Permission denied.*runs as uid"):
            artwork.save_artist_image("ヨルシカ", png())
        assert not (locked / "folder.jpg.tmp").exists()

        src = FakeSource("src", [])
        src.artist_images = lambda q: [ImageCandidate("https://x/1.jpg", "src", entity="1")]
        _with_sources(artwork, src)
        monkeypatch.setattr(artwork, "download", lambda url: png())
        job_id = artwork.state.create_job("artists-fill", "t", {})
        ctx = JobContext(artwork.state, job_id, {})
        artwork._job_fill_artists(ctx)  # must not raise
        ctx.flush()
        log = artwork.state.job(job_id)["log"]
        assert "Permission denied" in log
        assert artwork.artist_info("Writable")["image"] is not None
    finally:
        locked.chmod(0o755)


def test_embedart_remove_art_file_setup(env, fake_source, artwork, monkeypatch):
    """embedart auto + remove_art_file: the cover file vanishes during set_art."""
    from beets import plugins
    from mediafile import Image, MediaFile

    from beets_ui.jobs import JobContext

    import_asis(env, "a", artist="Embed", album="Only")
    album = beets_ctx.lib().albums()[0]
    real_send = plugins.send

    def fake_send(event, **kw):
        if event == "art_set":  # what embedart does with remove_art_file: yes
            a = kw["album"]
            art = os.fsdecode(a.artpath)
            for item in a.items():
                mf = MediaFile(os.fsdecode(item.path))
                mf.images = [Image(data=Path(art).read_bytes())]
                mf.save()
            os.remove(art)
            a.artpath = None
            a.store()
            return []
        return real_send(event, **kw)

    monkeypatch.setattr(plugins, "send", fake_send)
    monkeypatch.setattr(artwork, "embed_only", lambda: True)
    monkeypatch.setattr(
        artwork, "find_album_covers", lambda a, s=None: [{"url": "https://x/c.jpg", "source": "X"}]
    )
    monkeypatch.setattr(artwork, "download", lambda url: png(800, 800))

    assert artwork.save_album_cover(album, png(800, 800)) is None
    st = artwork.cover_status(beets_ctx.lib().get_album(album.id))
    assert st["embedded"] and st["size"] == (800, 800) and not st["low"]

    # A second album without art: the fill job downloads, beets embeds, no crash.
    import_asis(env, "b", artist="Embed", album="Two")
    job_id = artwork.state.create_job("covers-fill", "t", {"extract": True})
    ctx = JobContext(artwork.state, job_id, {"extract": True})
    artwork._job_fill_covers(ctx)
    ctx.flush()
    log = artwork.state.job(job_id)["log"]
    assert "Embed - Two (X, embedded)" in log
    assert "extracted" not in log.split("Done:")[0]  # embedded art counts as complete
    assert "1 covers downloaded" in log

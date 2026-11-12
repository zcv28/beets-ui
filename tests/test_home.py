"""Home: what needs you, what is coming in, what is missing, how the library is doing."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from beets_ui import beets_ctx, importer
from beets_ui.settings import Settings

from conftest import make_album


@pytest.fixture
def app(env, fake_source):
    from beets_ui.main import create_app

    return create_app(Settings.from_env(), background=False)


@pytest.fixture
def client(app):
    c = TestClient(app)
    assert c.post("/login", data={"password": "secret"}, follow_redirects=False).status_code == 303
    return c


def test_home_is_the_start_page(app, client):
    r = TestClient(app).get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    page = client.get("/").text
    nav = page.split('aria-label="Sections"')[1].split("</nav>")[0]
    assert nav.index('href="/"') < nav.index('href="/inbox"')
    assert "Nothing needs you right now." in page and "Nothing is downloading." in page
    assert "Your library is empty." in page


def test_needs_you_and_coming_in(app, client, env):
    state = app.state.state
    make_album(env["inbox"] / "Some rip")
    app.state.inbox.scan()
    state.upsert_folder("Some rip", status="ready")
    state.add_wanted(
        kind="missing",
        artist="A",
        album="Choose Me",
        status="choosing",
        status_text="No copy was clearly right",
    )
    state.add_wanted(
        kind="missing",
        artist="A",
        album="Never Found",
        status="failed",
        status_text="Nothing found",
    )
    state.add_wanted(
        kind="missing",
        artist="A",
        album="On Its Way",
        status="searching",
        status_text="Searching for A On Its Way",
    )
    page = client.get("/").text
    assert "1 folder to review" in page and "/inbox/folder?f=Some%20rip" in page
    assert "Pick a copy of Choose Me" in page and "Couldn&#39;t get Never Found" in page
    assert "On Its Way" in page and "Searching for A On Its Way" in page
    # The left column reloads on its own.
    now = client.get("/home/now").text
    assert "Pick a copy of Choose Me" in now and "<html" not in now


def test_missing_and_library_care(app, client, env):
    folder = make_album(env["tmp"] / "seed" / "Test Album")
    key = importer.preview_folder(str(folder))[0]["key"]
    importer.apply_folder(str(folder), {key: {"action": "apply", "id": "fake-1", "source": "Fake"}})
    album = beets_ctx.lib().albums()[0]
    album["data_source"] = "VocaDB"
    album["mb_albumartistid"] = "77"
    album.store()
    rels = [
        {
            "id": str(i),
            "title": f"EP {i}",
            "original": "",
            "kind": "ep",
            "type": "EP",
            "date": "2020-01-01",
            "catalog": "",
            "artists": "",
            "url": "",
            "names": [f"EP {i}"],
        }
        for i in (1, 2)
    ]
    app.state.state.set_discography("vocadb", "77", "Test Artist", rels)
    page = client.get("/").text
    assert "2 releases by 1 artist" in page and "/library/artist?name=Test%20Artist" in page
    # Lossy 128 kbps test files, no cover file, no synced lyrics.
    assert "/insights/upgrades?reason=lossy" in page and "/library/covers?filter=nofile" in page
    assert '>0%</span> <span class="hint">of tracks' in page
    assert "Recently added" in page and "Test Album" in page

"""A cleaner library: artist pages with what's missing, a quieter album page."""

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


def _import(env, folder="seed", **kw):
    path = make_album(env["tmp"] / "seed" / folder, **kw)
    key = importer.preview_folder(str(path))[0]["key"]
    importer.apply_folder(str(path), {key: {"action": "apply", "id": "fake-1", "source": "Fake"}})
    return beets_ctx.lib().albums()[-1]


def test_artist_page_lists_albums_and_what_is_missing(client, app, env):
    album = _import(env)
    album["data_source"] = "VocaDB"
    album["mb_albumartistid"] = "77"
    album.store()
    rel = {
        "id": "500",
        "title": "New EP",
        "original": "新しいEP",
        "kind": "ep",
        "type": "EP",
        "date": "2020-01-01",
        "catalog": "",
        "artists": "",
        "url": "https://vocadb.net/Al/500",
        "names": ["New EP"],
    }
    app.state.state.set_discography("vocadb", "77", "Test Artist", [rel])

    page = client.get("/library/artist", params={"name": "Test Artist"})
    assert page.status_code == 200
    text = page.text
    assert "Test Album" in text and "Missing" in text and "New EP" in text and "新しいEP" in text
    # Without a download source there is nothing to Get, but you can still dismiss.
    assert 'action="/wanted/release"' not in text and "Dismiss New EP" in text
    assert client.get("/library/artist", params={"name": "Nobody"}).status_code == 404


def test_artist_page_says_when_not_looked_up(client, app, env):
    album = _import(env)
    album["data_source"] = "VocaDB"
    album["mb_albumartistid"] = "77"
    album.store()
    text = client.get("/library/artist", params={"name": "Test Artist"}).text
    assert "hasn't been looked up yet" in text and "Look up on VocaDB" in text


def test_artist_links_go_to_the_artist_page(client, env):
    album = _import(env)
    assert "/library/artist?name=Test%20Artist" in client.get("/library/artists").text
    assert "/library/artist?name=Test%20Artist" in client.get(f"/library/album/{album.id}").text


def test_album_page_offers_a_better_copy_when_lossy(client, env):
    album = _import(env)  # the test files are 128 kbps MP3s
    text = client.get(f"/library/album/{album.id}").text
    assert "A better copy may exist" in text and "Low bitrate: MP3 128 kbps." in text
    assert "Lossy:" not in text  # said once, as the low bitrate
    assert "3 tracks" in text and '<summary class="hint">Details</summary>' in text


def test_tracks_and_tidy_up(client, env):
    _import(env)
    tracks = client.get("/library/tracks").text
    assert "Singletons</a>" in tracks and 'href="/library/tracks?singletons=1"' in tracks
    tabs = tracks.split('aria-label="Library views"')[1].split("</nav>")[0]
    assert "Singletons" not in tabs
    artists = client.get("/library/artists").text
    assert "Tidy up" in artists and "/library/artists/aliases" in artists

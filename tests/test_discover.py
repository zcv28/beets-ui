"""Discover: ListenBrainz playlists and fresh releases, filtered against the library."""

from __future__ import annotations

import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from beets_ui import beets_ctx, discover, importer
from beets_ui.acquire import Acquirer
from beets_ui.discography import Client
from beets_ui.settings import Settings

from conftest import make_album
from test_get import FakeClient, FakeSource

LB = "https://api.listenbrainz.org"


def _playlist(mbid, patch, title, date):
    return {
        "playlist": {
            "identifier": f"https://listenbrainz.org/playlist/{mbid}",
            "title": title,
            "date": date,
            "extension": {
                "https://musicbrainz.org/doc/jspf#playlist": {
                    "additional_metadata": {"algorithm_metadata": {"source_patch": patch}}
                }
            },
        }
    }


def _track(rec, title, artist):
    return {
        "identifier": [f"https://musicbrainz.org/recording/{rec}"],
        "title": title,
        "creator": artist,
    }


def _mock_listenbrainz():
    respx.get(f"{LB}/1/user/tester/playlists/createdfor").mock(
        return_value=httpx.Response(
            200,
            json={
                "playlists": [
                    _playlist("p-jams", "weekly-jams", "Weekly Jams", "2026-11-09T00:00:00"),
                    _playlist(
                        "p-old", "weekly-exploration", "Old Exploration", "2026-10-01T00:00:00"
                    ),
                    _playlist(
                        "p-new", "weekly-exploration", "Weekly Exploration", "2026-11-09T00:00:00"
                    ),
                    _playlist("p-other", "something-else", "Other", "2026-11-09T00:00:00"),
                ]
            },
        )
    )
    respx.get(f"{LB}/1/playlist/p-new").mock(
        return_value=httpx.Response(
            200,
            json={
                "playlist": {
                    "track": [
                        _track("r1", "Lydia", "Dauwd"),
                        _track("r2", "Kanade", "Yorushika"),
                        _track("r3", "Ghost", "DECO*27"),
                    ]
                }
            },
        )
    )
    respx.get(f"{LB}/1/playlist/p-jams").mock(
        return_value=httpx.Response(
            200, json={"playlist": {"track": [_track("r4", "Hana", "Yorushika")]}}
        )
    )

    def metadata(request):
        out = {
            "r1": {
                "release": {
                    "release_group_mbid": "rg-mix",
                    "mbid": "rel-mix",
                    "name": "Connecting The Dots",
                    "album_artist_name": "Michael Mayer",
                    "year": 2021,
                }
            },
            "r2": {
                "release": {
                    "release_group_mbid": "rg-elma",
                    "mbid": "rel-elma",
                    "name": "Elma",
                    "album_artist_name": "Yorushika",
                    "year": 2019,
                }
            },
            "r3": {
                "release": {
                    "release_group_mbid": "rg-ghost",
                    "mbid": "rel-ghost",
                    "name": "GHOST",
                    "album_artist_name": "DECO*27",
                    "year": 2016,
                }
            },
            "r4": {
                "release": {
                    "release_group_mbid": "rg-elma",
                    "mbid": "rel-elma",
                    "name": "Elma",
                    "album_artist_name": "Yorushika",
                    "year": 2019,
                }
            },
        }
        ids = request.url.params["recording_mbids"].split(",")
        return httpx.Response(200, json={i: out[i] for i in ids if i in out})

    respx.get(f"{LB}/1/metadata/recording/").mock(side_effect=metadata)
    respx.get(f"{LB}/1/user/tester/fresh_releases").mock(
        return_value=httpx.Response(
            200,
            json={
                "payload": {
                    "releases": [
                        {
                            "release_group_mbid": "rg-new",
                            "release_mbid": "x",
                            "release_name": "New Album",
                            "artist_credit_name": "Yorushika",
                            "release_date": "2026-11-01",
                            "release_group_primary_type": "Album",
                            "release_group_secondary_type": None,
                        },
                        {
                            "release_group_mbid": "rg-single",
                            "release_mbid": "y",
                            "release_name": "A Single",
                            "artist_credit_name": "Yorushika",
                            "release_date": "2026-11-02",
                            "release_group_primary_type": "Single",
                            "release_group_secondary_type": None,
                        },
                        {
                            "release_group_mbid": "rg-best",
                            "release_mbid": "z",
                            "release_name": "Best Of",
                            "artist_credit_name": "DECO*27",
                            "release_date": "2026-10-20",
                            "release_group_primary_type": "Album",
                            "release_group_secondary_type": "Compilation",
                        },
                        {
                            "release_group_mbid": "rg-later",
                            "release_mbid": "w",
                            "release_name": "Later",
                            "artist_credit_name": "DECO*27",
                            "release_date": "2099-01-01",
                            "release_group_primary_type": "EP",
                            "release_group_secondary_type": None,
                        },
                    ]
                }
            },
        )
    )


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(discover, "INTERVAL", 0)


@respx.mock
def test_sync_turns_playlists_into_albums(tmp_path):
    from beets_ui.state import State

    _mock_listenbrainz()
    state = State(tmp_path / "state.db")
    info = discover.sync(state, Client(), "tester")
    # The newest playlist of each known kind; exploration is read first.
    assert [p["label"] for p in info["playlists"]] == ["Weekly Exploration", "Weekly Jams"]
    albums = {a["id"]: a for a in info["albums"]}
    assert set(albums) == {"rg-mix", "rg-elma", "rg-ghost"}
    # Two tracks of the same album, from two playlists, make one suggestion.
    assert [t["title"] for t in albums["rg-elma"]["tracks"]] == ["Kanade", "Hana"]
    assert albums["rg-elma"]["lists"] == ["Weekly Exploration", "Weekly Jams"]
    assert info["albums"][0]["id"] == "rg-elma"  # exploration, most tracks
    # A track on someone else's album says so.
    assert albums["rg-mix"]["artist"] == "Michael Mayer"
    kinds = {r["id"]: r["kind"] for r in info["fresh"]}
    assert kinds == {
        "rg-new": "album",
        "rg-single": "single",
        "rg-best": "compilation",
        "rg-later": "ep",
    }
    assert state.meta(discover.META_KEY)["user"] == "tester"


@respx.mock
def test_unknown_user_says_so(tmp_path):
    from beets_ui.state import State

    respx.get(f"{LB}/1/user/nobody/playlists/createdfor").mock(return_value=httpx.Response(404))
    with pytest.raises(discover.ListenBrainzError, match="no user named nobody"):
        discover.sync(State(tmp_path / "s.db"), Client(), "nobody")


# ------------------------------------------------------------------ the page


@pytest.fixture
def app(env, fake_source, monkeypatch):
    from beets_ui.main import create_app

    monkeypatch.setenv("LISTENBRAINZ_USER", "tester")
    app = create_app(Settings.from_env(), background=False)
    src = FakeSource()
    acq = Acquirer.build(app.state.state, app.state.inbox, app.state.settings, src, None)
    acq.client_factory = FakeClient
    app.state.source = src
    app.state.acquirer = acq
    return app


@pytest.fixture
def client(app):
    c = TestClient(app)
    assert c.post("/login", data={"password": "secret"}, follow_redirects=False).status_code == 303
    return c


@respx.mock
def test_discover_page_leaves_out_what_you_have(app, client):
    _mock_listenbrainz()
    state = app.state.state
    discover.sync(state, Client(), "tester")
    # GHOST by DECO*27 is in the library already.
    make = make_album(app.state.settings.inbox / "seed")
    key = importer.preview_folder(str(make))[0]["key"]
    importer.apply_folder(str(make), {key: {"action": "apply", "id": "fake-1", "source": "Fake"}})
    a = beets_ctx.lib().albums()[0]
    a["albumartist"], a["album"] = "DECO*27", "GHOST"
    a.store()

    page = client.get("/get/discover").text
    assert "Elma" in page and "Connecting The Dots" in page
    assert "GHOST" not in page
    assert "Kanade" in page and "Weekly Exploration" in page
    # New releases: albums and EPs by default, upcoming apart.
    assert "New Album" in page and "A Single" not in page and "Later" in page
    assert "Best Of" not in page
    assert "A Single" in client.get("/get/discover?kinds=single").text
    assert 'href="/get/discover" aria-current="page"' in page

    # Get one: a MusicBrainz request, shown as its state from then on.
    r = client.post(
        "/wanted/discover",
        data={"id": "rg-elma", "title": "Elma", "artist": "Yorushika", "date": "2019"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "wanted-state" in r.text
    w = state.wanted_list()[0]
    assert (w["source"], w["release_id"], w["album"]) == ("musicbrainz", "rg-elma", "Elma")
    page = client.get("/get/discover").text
    assert f'href="/wanted/{w["id"]}"' in page

    # Dismiss one; it can be suggested again.
    client.post("/get/discover/dismiss", data={"id": "rg-mix", "label": "Connecting The Dots"})
    page = client.get("/get/discover").text
    assert "Connecting The Dots" not in page and "1 album dismissed" in page
    client.post("/get/discover/restore-all")
    assert "Connecting The Dots" in client.get("/get/discover").text


def test_discover_page_reads_when_due_and_explains_setup(app, client, env, monkeypatch):
    client.get("/get/discover")
    jobs = app.state.state.jobs(5, kind=discover.SYNC_KIND)
    assert len(jobs) == 1  # read on the first visit
    client.get("/get/discover")
    assert len(app.state.state.jobs(5, kind=discover.SYNC_KIND)) == 1  # not twice
    app.state.state.set_meta(discover.META_KEY, {"at": time.time(), "user": "tester"})
    assert "Nothing new to suggest" not in client.get("/get/discover").text

    from beets_ui.main import create_app

    monkeypatch.delenv("LISTENBRAINZ_USER")
    bare = TestClient(create_app(Settings.from_env(), background=False))
    bare.post("/login", data={"password": "secret"})
    assert "LISTENBRAINZ_USER" in bare.get("/get/discover").text

"""Get: requests are searched, downloaded and imported, or wait for you."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from beets_ui import acquire, beets_ctx, discography, importer
from beets_ui.acquire import Acquirer
from beets_ui.rank import rank, score
from beets_ui.settings import Settings
from beets_ui.sources.base import Source

from conftest import make_album

# --------------------------------------------------------------- fakes


def offer(origin, folder_name, tracks=3, lossless=True, bitrate=None, length=1, **kw):
    folder = f"Music/{folder_name}"
    ext = "flac" if lossless else "mp3"
    files = [
        {
            "filename": f"Music\\{folder_name}\\{i:02d}.{ext}",
            "name": f"{i:02d}.{ext}",
            "size": 1000,
            "bitrate": bitrate,
            "length": length,
            "ext": ext,
            "audio": True,
        }
        for i in range(1, tracks + 1)
    ]
    return {
        "origin": origin,
        "folder": folder,
        "path": f"Music\\{folder_name}",
        "folder_name": folder_name,
        "files": files,
        "format": ext.upper(),
        "bitrate": bitrate,
        "lossless": lossless,
        "tracks": tracks,
        "size": 1000 * tracks,
        "free_slot": True,
        "queue": 0,
        "speed": 1_000_000,
        "locked": False,
        **kw,
    }


class FakeSource(Source):
    name = "fake"
    label = "Fake"

    def __init__(self):
        self.offered: list = []
        self.listings: dict = {}
        self.fetched: list = []
        self.rows: list = []
        self.running = False
        self.searched: list = []

    def status(self):
        return {"reachable": True, "connected": True, "error": ""}

    def search(self, text, **opts):
        self.searched.append(text)
        return f"s{len(self.searched)}"

    def search_info(self, search_id):
        return {"id": search_id, "text": "", "running": self.running, "label": "", "answers": 1}

    def offers(self, search_id, audio_only=True):
        return self.offered

    def listing(self, origin, path):
        return self.listings.get((origin, path), [])

    def fetch(self, origin, files):
        self.fetched.append((origin, len(files)))

    def transfers(self):
        return self.rows

    def cancel(self, origin, ids, remove=False):
        pass


def transfer(o, state):
    return {
        "origin": o["origin"],
        "folder": o["folder"],
        "state": state,
        "label": state.title(),
        "percent": 100 if state == "done" else 50,
        "place": None,
        "files": [],
    }


class FakeClient:
    """Answers the tracklist lookup with three one-second tracks."""

    def get(self, url, params, interval):
        return {"tracks": [{"name": f"Track {i}", "song": {"lengthSeconds": 1}} for i in (1, 2, 3)]}


@pytest.fixture
def app(env, fake_source):
    from beets_ui.main import create_app

    return create_app(Settings.from_env(), background=False)


@pytest.fixture
def engine(app):
    src = FakeSource()
    acq = Acquirer.build(app.state.state, app.state.inbox, app.state.settings, src, None)
    acq.client_factory = FakeClient
    return acq, src


def want(state, **kw):
    fields = dict(
        kind="missing",
        artist="Test Artist",
        album="Test Album",
        source="vocadb",
        release_id="fake-1",
        lookup_id="fake-1",
    )
    fields.update(kw)
    return state.add_wanted(**fields)


# ---------------------------------------------------------------- ranking


def test_rank_takes_the_complete_lossless_copy():
    w = {
        "artist": "Test Artist",
        "album": "Test Album",
        "artist_original": "",
        "album_original": "",
        "tracks": [{"title": "a", "length": 60}] * 3,
    }
    picks = rank(
        [
            offer("short", "Test Artist - Test Album", tracks=2, length=60),
            offer("mp3", "Test Album [MP3]", lossless=False, bitrate=320, length=60),
            offer("flac", "Test Artist - Test Album (FLAC)", length=60),
            offer("other", "Something Else", length=60),
        ],
        w,
    )
    assert [p.offer["origin"] for p in picks][:2] == ["flac", "mp3"]
    assert picks[0].confident and picks[1].confident
    assert not next(p for p in picks if p.offer["origin"] == "short").confident
    assert all(p.offer["origin"] != "other" for p in picks)  # album not named: left out


def test_rank_upgrades_need_a_better_copy():
    base = {
        "artist": "A",
        "album": "Album Name",
        "artist_original": "",
        "album_original": "",
        "tracks": [{"title": "a", "length": 0}] * 3,
    }
    lossy_now = {**base, "min_bitrate": 192}
    assert not score(offer("x", "A - Album Name", lossless=False, bitrate=192), lossy_now).confident
    assert score(offer("x", "A - Album Name", lossless=False, bitrate=320), lossy_now).confident
    lossless_wanted = {**base, "want_lossless": True}
    assert not score(
        offer("x", "A - Album Name", lossless=False, bitrate=320), lossless_wanted
    ).confident


def test_rank_without_tracklist_needs_the_artist():
    w = {
        "artist": "Yorushika",
        "album": "Elma",
        "artist_original": "ヨルシカ",
        "album_original": "エルマ",
        "tracks": [],
    }
    assert score(offer("x", "ヨルシカ - エルマ"), w).confident
    assert not score(offer("x", "Elma OST"), w).confident


def test_queries_try_both_scripts():
    w = {
        "artist": "Yorushika",
        "album": "Elma",
        "artist_original": "ヨルシカ",
        "album_original": "エルマ",
    }
    assert acquire.queries(w) == ["Yorushika Elma", "ヨルシカ エルマ", "Yorushika エルマ"]


@respx.mock
def test_musicbrainz_tracklist_picks_the_usual_release():
    def release(rid, n, date):
        return {
            "id": rid,
            "status": "Official",
            "date": date,
            "media": [{"tracks": [{"title": f"T{i}", "length": 60000} for i in range(n)]}],
        }

    respx.get(url__startswith="https://musicbrainz.org/ws/2/release").mock(
        return_value=httpx.Response(
            200,
            json={
                "releases": [
                    release("deluxe", 14, "2020-01-01"),
                    release("late", 10, "2021-01-01"),
                    release("first", 10, "2019-05-01"),
                ]
            },
        )
    )
    client = discography.Client(httpx.Client())
    tracks, lookup = acquire.tracklist(client, "musicbrainz", "group-id")
    assert lookup == "first" and len(tracks) == 10 and tracks[0]["length"] == 60


# ---------------------------------------------------------------- engine


def test_get_searches_downloads_and_imports(app, engine, env):
    acq, src = engine
    state = app.state.state
    wid = want(state)
    good = offer("alice", "Test Artist - Test Album")
    src.offered = [offer("bob", "Test Album", tracks=2), good]

    acq.tick()  # tracklist, then search
    w = state.wanted(wid)
    assert w["status"] == "searching" and len(w["tracks"]) == 3
    assert src.searched == ["Test Artist Test Album"]

    acq.tick()  # search done: the complete copy is taken
    w = state.wanted(wid)
    assert w["status"] == "downloading" and w["origin"] == "alice"
    assert src.fetched == [("alice", 3)]

    # The download finishes and its folder appears in the inbox.
    src.rows = [transfer(good, "done")]
    make_album(env["inbox"] / "Test Artist - Test Album")
    acq.tick()
    assert state.wanted(wid)["status"] == "arrived"
    acq.tick()  # linked, looked up by the release that was asked for
    assert state.wanted(wid)["status"] == "lookup"
    assert state.folder("Test Artist - Test Album")["wanted_id"] == wid
    job = state.jobs(1)[0]
    assert state.job(job["id"])["params"]["ids"] == ["fake-1"]

    app.state.jobs.run_pending_sync()
    assert state.wanted(wid)["status"] == "done"
    assert [a.album for a in beets_ctx.lib().albums()] == ["Test Album"]


def test_no_clear_copy_means_choosing(app, engine):
    acq, src = engine
    state = app.state.state
    wid = want(state)
    src.offered = [offer("bob", "Test Album", tracks=2)]
    for _ in range(8):  # every search text, then give up
        acq.tick()
    w = state.wanted(wid)
    assert w["status"] == "choosing"
    assert [o["origin"] for o in w["offers"]] == ["bob"]
    assert src.fetched == []
    # You pick it: it downloads whatever its score.
    assert acq.choose(w, "bob", "Music/Test Album")
    assert state.wanted(wid)["status"] == "downloading"


def test_nothing_found_fails(app, engine):
    acq, src = engine
    wid = want(app.state.state)
    for _ in range(8):
        acq.tick()
    w = app.state.state.wanted(wid)
    assert w["status"] == "failed" and w["status_text"] == "Nothing found"


def test_failed_download_moves_to_the_next_copy(app, engine):
    acq, src = engine
    state = app.state.state
    wid = want(state)
    first = offer("alice", "Test Artist - Test Album")
    second = offer("carol", "Test Album (Test Artist) FLAC")
    src.offered = [first, second]
    acq.tick()
    acq.tick()
    assert state.wanted(wid)["origin"] == "alice"
    src.rows = [transfer(first, "failed")]
    acq.tick()
    w = state.wanted(wid)
    assert w["status"] == "downloading" and w["origin"] == "carol" and w["attempts"] == 2


def test_two_searches_at_a_time(app, engine):
    acq, src = engine
    state = app.state.state
    ids = [want(state, release_id=str(i)) for i in range(3)]
    src.running = True
    acq.tick()
    assert [state.wanted(i)["status"] for i in ids] == ["searching", "searching", "queued"]


def test_upgrade_replaces_the_old_album(app, engine, env):
    acq, src = engine
    state = app.state.state
    old = make_album(env["tmp"] / "old" / "Test Album")
    key = importer.preview_folder(str(old))[0]["key"]
    importer.apply_folder(str(old), {key: {"action": "apply", "id": "fake-1", "source": "Fake"}})
    album = beets_ctx.lib().albums()[0]
    wid, created = acquire.from_album(state, album)
    assert created and state.wanted(wid)["min_bitrate"] > 0  # the copy is lossy
    # Its download is here: it is looked up by the album's ID and replaces the album.
    state.update_wanted(wid, status="arrived", folder="Better copy", lookup_id="fake-1")
    make_album(env["inbox"] / "Better copy")
    acq.tick()
    app.state.jobs.run_pending_sync()
    assert state.wanted(wid)["status"] == "done"
    albums = beets_ctx.lib().albums()
    assert len(albums) == 1 and albums[0].id != album.id


def test_a_folder_that_does_not_fit_waits_for_review(app, engine, env):
    acq, src = engine
    state = app.state.state
    wid = want(state)
    state.update_wanted(wid, status="arrived", folder="Test Album rip")
    make_album(env["inbox"] / "Test Album rip", n=5)  # two files more than the release has
    acq.tick()
    app.state.jobs.run_pending_sync()
    w = state.wanted(wid)
    assert w["status"] == "review" and "match no track" in w["status_text"]
    assert len(beets_ctx.lib().albums()) == 0


def test_a_different_release_is_not_imported_as_the_request():
    from beets_ui.inbox import _not_the_release

    tasks = [{"candidates": [{"id": "other", "album": "Other"}]}]
    assert "not the album asked for" in _not_the_release(tasks, ["asked"])
    assert _not_the_release(tasks, ["other"]) == ""
    assert _not_the_release(tasks, []) == ""


# ------------------------------------------------------------------ pages


@pytest.fixture
def client(app, engine):
    acq, src = engine
    app.state.source = src
    app.state.acquirer = acq
    c = TestClient(app)
    assert c.post("/login", data={"password": "secret"}, follow_redirects=False).status_code == 303
    return c


def test_get_from_the_gaps_page(client, app):
    state = app.state.state
    lib = beets_ctx.lib()
    make = make_album(app.state.settings.inbox / "seed")
    key = importer.preview_folder(str(make))[0]["key"]
    importer.apply_folder(str(make), {key: {"action": "apply", "id": "fake-1", "source": "Fake"}})
    a = lib.albums()[0]
    a["data_source"] = "VocaDB"
    a["mb_albumartistid"] = "77"
    a.store()
    rel = {
        "id": "500",
        "title": "New EP",
        "original": "",
        "kind": "ep",
        "type": "EP",
        "date": "2020-01-01",
        "catalog": "",
        "artists": "",
        "url": "",
        "names": ["New EP"],
    }
    state.set_discography("vocadb", "77", "Test Artist", [rel])

    page = client.get("/insights/gaps").text
    assert 'action="/wanted/release"' in page and "Get 1 missing" in page
    r = client.post(
        "/wanted/release",
        data={"source": "vocadb", "artist_id": "77", "release_id": "500"},
        headers={"HX-Request": "true"},
    )
    assert "Waiting to search" in r.text
    # The pop-up header must not bring the empty pop-up response's length along.
    assert r.headers["content-length"] == str(len(r.content))
    assert json.loads(r.headers["HX-Trigger"])["toast"]["title"] == "Getting New EP"
    # Asking again reuses the request.
    client.post(
        "/wanted/release", data={"source": "vocadb", "artist_id": "77", "release_id": "500"}
    )
    assert len(state.wanted_list()) == 1
    # The inbox lists it, and its page says where it stands.
    assert "New EP" in client.get("/inbox").text
    wid = state.wanted_list()[0]["id"]
    assert "Waiting to search" in client.get(f"/wanted/{wid}").text


def test_request_page_lets_you_choose(client, app):
    state = app.state.state
    wid = want(state)
    o = offer("bob", "Test Album", tracks=2)
    state.update_wanted(
        wid,
        status="choosing",
        status_text="No copy was clearly right",
        offers=[
            {
                k: o[k]
                for k in (
                    "origin",
                    "folder",
                    "path",
                    "folder_name",
                    "files",
                    "format",
                    "bitrate",
                    "lossless",
                    "tracks",
                    "size",
                )
            }
            | {"why": ["2 of 3 tracks"], "confident": False}
        ],
        tracks=[{"title": "t", "length": 1}] * 3,
    )
    page = client.get(f"/wanted/{wid}").text
    assert "Download this one" in page and "2 of 3 tracks" in page
    r = client.post(
        f"/wanted/{wid}/choose",
        data={"origin": "bob", "folder": "Music/Test Album"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/inbox"
    assert state.wanted(wid)["status"] == "downloading"


def test_cancel_retry_remove(client, app):
    state = app.state.state
    wid = want(state)
    client.post(f"/wanted/{wid}/cancel")
    assert state.wanted(wid)["status"] == "cancelled"
    client.post(f"/wanted/{wid}/retry")
    assert state.wanted(wid)["status"] == "queued"
    client.post(f"/wanted/{wid}/remove")
    assert state.wanted(wid) is None


def test_request_outcomes_pop_up(client, app):
    import time

    state = app.state.state
    since = time.time() - 1
    wid = want(state)
    state.update_wanted(wid, status="done", status_text="In your library")
    r = client.get(f"/jobs/pill?since={since}")
    assert "Got " in r.text and "Test Album" in r.text

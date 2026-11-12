"""The inbox waits for downloads, requests don't get stuck on a deleted folder, both
spellings of a release are searched before a copy is taken, covers in Discover and
the inbox, the Find filter, and a request page whose links keep working."""

# The Get tests' fixtures are imported below, which pytest needs and ruff reads as redefinitions.
# ruff: noqa: F811

from __future__ import annotations

import re
import shutil
import time

import httpx
import respx

from beets_ui import acquire, beets_ctx, discover, sources
from beets_ui.inbox import folder_cover
from beets_ui.rank import rank
from beets_ui.release_covers import MB_RELEASE, ReleaseCovers, candidate_ref, path_for
from beets_ui.sources import Watcher

from conftest import make_album

# The fakes and fixtures of the Get tests (pytest finds fixtures imported here).
from test_get import app, client, engine, offer, transfer, want  # noqa: F401

FRAGMENT = {"HX-Request": "true"}
MBID = "0e2d1f4c-7c5b-4a51-9d3e-2f6f0a1b2c3d"


def download(o, state):
    """A download row as the source lists it, for an offer."""
    return {**transfer(o, state), "folder_name": o["folder_name"], "speed": 0, "size": 3000}


def watch_downloads(app, src):
    """The app follows the source's downloads, as it does when one is set up."""
    app.state.source_watch = Watcher(src)
    app.state.inbox.in_flight = lambda: sources.folders_in_flight(
        app.state.state, app.state.source_watch
    )
    app.state.source_watch.poll()


# ----------------------------------------------------- the request page


def test_links_inside_a_request_page_do_not_inherit_its_reload(client, app):
    """The page reloads part of itself with hx-select. Inherited by the links and
    buttons inside, that cut the next page down to nothing: a blank screen."""
    wid = want(app.state.state, status="failed", status_text="Nothing found")
    page = client.get(f"/wanted/{wid}").text
    live = re.search(r'<div id="request-live"[^>]*>', page).group(0)
    assert 'hx-select="#request-live"' in live
    assert 'hx-disinherit="hx-select hx-swap"' in live
    # No other page hands hx-select down to what it contains.
    for url in ("/", "/get/coming", "/get/missing", "/inbox", "/jobs"):
        for tag in re.findall(r"<[^>]*hx-select=[^>]*>", client.get(url).text):
            assert "hx-disinherit" in tag, (url, tag)


# ------------------------------------------------- both spellings, best copy


def yorushika(state):
    return want(
        state, artist="Yorushika", album="Elma", artist_original="ヨルシカ", album_original="エルマ"
    )


def test_both_spellings_are_searched_before_a_copy_is_taken(app, engine):
    acq, src = engine
    state = app.state.state
    wid = yorushika(state)
    assert acquire.spellings(state.wanted(wid)) == 3
    mp3 = offer("roma", "Yorushika - Elma [320]", lossless=False, bitrate=320)
    flac = offer("kana", "ヨルシカ - エルマ (FLAC)")
    by_text = {"Yorushika Elma": [mp3], "ヨルシカ エルマ": [flac], "Yorushika エルマ": []}
    src.offers = lambda sid, audio_only=True: by_text[src.searched[int(sid[1:]) - 1]]

    acq.tick()  # the tracklist, then the romanized name
    acq.tick()  # a copy that fits: kept, not taken yet
    w = state.wanted(wid)
    assert w["status"] == "queued" and src.fetched == []
    assert w["status_text"] == "Searching under its other name too, to take the best copy"
    assert [o["origin"] for o in w["offers"]] == ["roma"]
    for _ in range(4):  # the name as written, then the mixed one
        acq.tick()
    w = state.wanted(wid)
    assert src.searched == ["Yorushika Elma", "ヨルシカ エルマ", "Yorushika エルマ"]
    # The lossless copy wins, though it was found under the other name.
    assert w["status"] == "downloading" and w["origin"] == "kana"
    assert src.fetched == [("kana", 3)]
    # The other copy is kept for when this one lets it down.
    assert [o["origin"] for o in w["offers"]] == ["kana", "roma"]


def test_one_spelling_takes_the_copy_at_once(app, engine):
    acq, src = engine
    wid = want(app.state.state)
    assert acquire.spellings(app.state.state.wanted(wid)) == 1
    src.offered = [offer("alice", "Test Artist - Test Album")]
    acq.tick()
    acq.tick()
    assert app.state.state.wanted(wid)["status"] == "downloading"


def test_better_audio_beats_a_sooner_copy():
    w = {
        "artist": "Test Artist",
        "album": "Test Album",
        "artist_original": "",
        "album_original": "",
        "tracks": [{"title": "a", "length": 60}] * 3,
    }
    soon = offer("soon", "Test Artist - Test Album [320]", lossless=False, bitrate=320, length=60)
    slow = offer("slow", "Test Album (FLAC)", length=60, free_slot=False, queue=80, speed=10_000)
    low = offer("low", "Test Artist - Test Album [V2]", lossless=False, bitrate=190, length=60)
    picks = rank([low, soon, slow], w)
    assert all(p.confident for p in picks)
    assert [p.offer["origin"] for p in picks] == ["slow", "soon", "low"]
    # The score alone would have taken the copy that arrives sooner.
    assert picks[1].score > picks[0].score


# ------------------------------------------------ a request's folder is gone


def reviewed(app, env, name="Test Album rip"):
    """A request whose download waits in the inbox for review."""
    state = app.state.state
    wid = want(state)
    make_album(env["inbox"] / name)
    app.state.inbox.scan()
    state.update_wanted(wid, status="review", status_text="In the inbox", folder=name)
    state.link_folder(name, wid)
    return wid


def test_deleting_the_folder_in_the_inbox_releases_its_request(client, app, env):
    state = app.state.state
    wid = reviewed(app, env)
    page = client.get("/get/coming").text
    assert "Waiting for review" in page
    client.post("/inbox/folder/delete", data={"f": "Test Album rip"})
    w = state.wanted(wid)
    assert w["status"] == "failed" and w["folder"] == ""
    assert w["status_text"] == "Its folder was deleted from the inbox"
    assert w["history"][-1]["text"] == f"{w['status_text']} before it was imported"
    # It asks you now: search again, find it by hand, or remove it.
    page = client.get(f"/wanted/{wid}").text
    assert "Its folder was deleted from the inbox" in page
    assert f'action="/wanted/{wid}/retry"' in page and f'action="/wanted/{wid}/remove"' in page
    home = client.get("/").text
    assert "Couldn&#39;t get Test Album" in home


def test_a_folder_deleted_by_hand_releases_its_request_on_the_next_scan(app, env):
    state = app.state.state
    wid = reviewed(app, env)
    shutil.rmtree(env["inbox"] / "Test Album rip")
    app.state.inbox.scan()
    assert state.wanted(wid)["status"] == "failed"
    # A request whose folder is still there is left alone.
    other = reviewed(app, env, "Another rip")
    app.state.inbox.scan()
    assert state.wanted(other)["status"] == "review"


def test_a_request_waiting_for_review_can_be_stopped(client, app, env):
    state = app.state.state
    wid = reviewed(app, env)
    page = client.get(f"/wanted/{wid}").text
    assert f'action="/wanted/{wid}/cancel"' in page
    assert "stays in the inbox as a folder like any other" in page
    client.post(f"/wanted/{wid}/cancel")
    assert state.wanted(wid)["status"] == "cancelled"
    # Its folder is an ordinary inbox folder again.
    assert state.folder("Test Album rip")["wanted_id"] is None
    # A lookup or an import that is running can't be stopped half way.
    busy = want(state, status="importing", release_id="other")
    r = client.post(f"/wanted/{busy}/cancel", headers=FRAGMENT)
    assert state.wanted(busy)["status"] == "importing"
    assert "can't be stopped right now" in r.headers["HX-Trigger"]


def test_a_failed_lookup_does_not_leave_the_request_looking_up(app, engine, env, monkeypatch):
    acq, _ = engine
    state = app.state.state
    wid = want(state)
    state.update_wanted(wid, status="arrived", folder="Broken rip")
    make_album(env["inbox"] / "Broken rip")

    def boom(*a, **k):
        raise RuntimeError("no answer from the metadata source")

    monkeypatch.setattr("beets_ui.importer.preview_folder", boom)
    acq.tick()
    assert state.wanted(wid)["status"] == "lookup"
    app.state.jobs.run_pending_sync()
    w = state.wanted(wid)
    assert w["status"] == "review" and "the lookup failed" in w["status_text"]


# ----------------------------------------- downloads that have not finished


def test_a_folder_still_downloading_is_not_up_for_review(client, app, engine, env, monkeypatch):
    _, src = engine
    state = app.state.state
    monkeypatch.setattr(app.state.settings, "auto_preview", True)
    monkeypatch.setattr(app.state.settings, "debounce", 0)
    monkeypatch.setattr(app.state.settings, "auto_import", 90)
    copy = offer("alice", "Half an album")
    make_album(env["inbox"] / "Half an album", n=2)  # two of its three files so far
    src.rows = [download(copy, "queued")]
    watch_downloads(app, src)
    assert sources.folders_in_flight(state, app.state.source_watch) == {"Half an album"}

    app.state.inbox.scan()
    app.state.jobs.run_pending_sync()
    # Not looked up, not asked of you, and shown as downloading.
    assert state.folder("Half an album")["status"] == "new"
    home = client.get("/").text
    assert "to review" not in home and "not looked up" not in home
    page = client.get("/inbox").text
    assert "Downloading · 1" in page and "Not looked up" not in page
    assert "Still downloading" in client.get("/inbox/folder?f=Half%20an%20album").text

    # Looked up all the same, it is neither imported on its own nor queued for review.
    job = app.state.inbox.queue_preview("Half an album", auto=True)
    app.state.jobs.run_pending_sync()
    assert "its download hasn't finished" in state.job(job)["log"]
    assert state.folder("Half an album")["status"] == "ready"
    assert len(beets_ctx.lib().albums()) == 0
    assert "to review" not in client.get("/").text

    # The download finishes: now it is looked up and imported like any other folder.
    make_album(env["inbox"] / "Half an album", n=3)
    src.rows = [download(copy, "done")]
    app.state.source_watch.poll()
    app.state.inbox.scan()
    app.state.jobs.run_pending_sync()
    app.state.jobs.run_pending_sync()
    assert [a.album for a in beets_ctx.lib().albums()] == ["Test Album"]


def test_a_download_between_two_uploaders_counts_as_running(app, engine):
    state = app.state.state
    state.set_meta(
        acquire.FETCHES_KEY,
        {"bob|Music/Rip": {**acquire.new_fetch("Rip", "s1", "rip", 3), "searching": "bob stalled"}},
    )
    assert sources.folders_in_flight(state, None) == {"Rip"}
    wid = want(state, status="downloading")
    state.update_wanted(wid, folder="Test Album FLAC")
    assert sources.folders_in_flight(state, None) == {"Rip", "Test Album FLAC"}


def test_an_earlier_folder_is_not_taken_for_the_finished_download(app, engine, env):
    """The list of downloads the engine reads may be a few seconds old. A download that
    only just started is not in it yet; a folder of the same name left in the inbox by an
    earlier copy must not pass for its result."""
    acq, src = engine
    state = app.state.state
    wid = want(state)
    copy = offer("alice", "Test Artist - Test Album")
    src.offered = [copy]
    acq.tick()
    acq.tick()
    assert state.wanted(wid)["status"] == "downloading"
    make_album(env["inbox"] / "Test Artist - Test Album", n=1)  # what an earlier copy left
    src.rows = [download(copy, "queued")]
    acq.transfers = lambda: []  # the stale list
    acq.tick()
    assert state.wanted(wid)["status"] == "downloading"
    # Cleared from the source's own list too: then the folder is the download.
    src.rows = []
    acq.tick()
    assert state.wanted(wid)["status"] == "arrived"


# ---------------------------------------------------------------- covers


def test_discover_shows_covers_and_lines_its_actions_up(client, app):
    state = app.state.state
    state.set_meta(
        discover.META_KEY,
        {
            "at": time.time(),
            "user": "me",
            "playlists": [{"label": "Weekly Jams", "title": "Weekly Jams", "date": ""}],
            "albums": [
                {
                    "id": MBID,
                    "release_id": "",
                    "title": "Suggested Album",
                    "artist": "Someone",
                    "year": "2020",
                    "tracks": [{"title": "A song", "artist": "Someone"}],
                    "lists": ["Weekly Jams"],
                }
            ],
            "fresh": [
                {
                    "id": MBID.replace("0e2d", "1e2d"),
                    "release_id": "",
                    "title": "Fresh Album",
                    "artist": "Someone",
                    "date": "2020-01-01",
                    "type": "Album",
                    "kind": "album",
                    "secondary": "",
                }
            ],
        },
    )
    app.state.settings.listenbrainz_user = "me"
    page = client.get("/get/discover").text
    assert f'src="/get/cover/musicbrainz/{MBID}"' in page  # the suggested album's cover
    assert f'src="/get/cover/musicbrainz/{MBID.replace("0e2d", "1e2d")}"' in page
    # Get (or the request's state) and the row's menu share one right-aligned line.
    assert page.count('<span class="row-actions">') == 2
    r = client.post(
        "/wanted/discover",
        data={"id": MBID, "title": "Suggested Album", "artist": "Someone"},
        headers=FRAGMENT,
    )
    assert "Waiting to search" in r.text


def test_an_inbox_folder_shows_the_cover_of_its_release():
    cand = {"id": MBID, "source": "MusicBrainz", "artist": "A", "album": "B", "match": 96.0}
    row = {"tasks": [{"key": "k", "is_album": True, "candidates": [cand]}], "decisions": {}}
    assert folder_cover(row) == {"src": f"/get/cover/{MB_RELEASE}/{MBID}", "label": "A – B"}
    # A weak match is no reason to show another album's cover.
    weak = {"tasks": [{"key": "k", "is_album": True, "candidates": [{**cand, "match": 60.0}]}]}
    assert folder_cover(weak) is None
    # The candidate you chose is the one shown, not the best one.
    other = {**cand, "id": "77", "source": "VocaDB", "album": "C", "match": 80.0}
    row = {
        "tasks": [{"key": "k", "is_album": True, "candidates": [cand, other]}],
        "decisions": {"k": {"action": "apply", "id": "77", "source": "VocaDB"}},
    }
    assert folder_cover(row) == {"src": "/get/cover/vocadb/77", "label": "A – C"}
    # Sources without covers by ID, and folders not looked up, show none.
    assert candidate_ref("Discogs", "123") is None and candidate_ref("Fake", "fake-1") is None
    assert folder_cover({"tasks": []}) is None
    # A folder you asked for shows the cover of the release that was asked for.
    asked = {
        "kind": "missing",
        "source": "vocadb",
        "release_id": "500",
        "artist": "X",
        "album": "Y",
    }
    assert folder_cover({"tasks": []}, asked) == {
        "src": path_for("vocadb", "500"),
        "label": "X – Y",
    }


def test_the_inbox_lists_covers(client, app, env, monkeypatch):
    make_album(env["inbox"] / "Looked up")
    make_album(env["inbox"] / "Not yet")
    app.state.inbox.scan()
    client.post("/inbox/folder/preview", data={"f": "Looked up"})
    app.state.jobs.run_pending_sync()
    # The fake lookup's source has no covers by ID: every folder shows the quiet placeholder.
    page = client.get("/inbox").text
    assert page.count("cover-none") == 2
    # Matched to a MusicBrainz release, the folder leads with that release's cover.
    row = app.state.state.folder("Looked up")
    row["tasks"][0]["candidates"][0].update(id=MBID, source="MusicBrainz")
    app.state.state.upsert_folder("Looked up", tasks=row["tasks"], decisions={})
    page = client.get("/inbox").text
    assert f'src="/get/cover/{MB_RELEASE}/{MBID}"' in page
    assert 'alt="Cover of Test Artist – Test Album"' in page
    assert page.count("cover-none") == 1


@respx.mock
def test_full_size_covers_come_from_the_same_release(tmp_path):
    covers = ReleaseCovers(tmp_path, httpx.Client())
    assert covers.full_url(MB_RELEASE, MBID) == f"https://coverartarchive.org/release/{MBID}/front"
    assert covers.full_url("musicbrainz", MBID).endswith(f"/release-group/{MBID}/front")
    respx.get("https://vocadb.net/api/albums/500").mock(
        return_value=httpx.Response(200, json={"mainPicture": {"urlOriginal": "https://x/500.jpg"}})
    )
    respx.get("https://vocadb.net/api/albums/501").mock(return_value=httpx.Response(200, json={}))
    assert covers.full_url("vocadb", "500") == "https://x/500.jpg"
    assert covers.full_url("vocadb", "501") is None
    assert covers.full_url("discogs", "1") is None


def test_an_album_imported_without_a_cover_gets_its_releases_own(client, app, env):
    asked = []

    def cover(album, source, release_id):
        asked.append((album.album, source, release_id))
        return f"Cover for {album.album}: the release's own"

    app.state.inbox.cover_from_release = cover
    make_album(env["inbox"] / "With a release")
    app.state.inbox.scan()
    client.post("/inbox/folder/preview", data={"f": "With a release"})
    app.state.jobs.run_pending_sync()
    # The lookup matched a VocaDB album: its number is what the album is imported against.
    state = app.state.state
    row = state.folder("With a release")
    assert row["tasks"][0]["candidates"][0]["id"] == "fake-1"
    client.post("/inbox/folder/import", data={"f": "With a release", "choice-0": "c:0"})
    app.state.jobs.run_pending_sync()
    assert len(beets_ctx.lib().albums()) == 1
    assert asked == []  # the fake source has no covers by ID: nothing is asked for

    album = beets_ctx.lib().albums()[0]
    album.mb_albumid = "500"
    album.store()
    from beets_ui.jobs import JobContext

    lines = []
    ctx = JobContext.__new__(JobContext)
    ctx.log = lines.append
    outcome = type("O", (), {"result": "imported"})()
    app.state.inbox._covers_from_releases(
        ctx, {"k": {"action": "apply", "id": "500", "source": "VocaDB"}}, {"k": outcome}
    )
    assert asked == [("Test Album", "vocadb", "500")]
    assert lines == ["Cover for Test Album: the release's own"]


# ------------------------------------------------------------------ find


def test_find_lists_searches_for_a_release_first(client, app, engine):
    _, src = engine
    state = app.state.state
    wid = want(state, artist="Yorushika", album="Elma")
    found = []

    def searches():
        return [
            {"id": i, "text": t, "running": False, "label": "Finished", "answers": 1, "files": 2}
            for i, t in found
        ]

    src.searches = searches
    found[:] = [("s2", "lofi beats"), ("s1", "yorushika elma")]
    state.log_search("s1", "yorushika elma", "Yorushika", "Elma", wid)
    page = client.get("/get/find").text
    assert 'aria-current="page">For a release · 1' in page
    assert "Elma" in page and "lofi beats" not in page
    page = client.get("/get/find?show=other").text
    assert 'aria-current="page">For nothing in particular · 1' in page
    assert "“lofi beats”" in page and "yorushika elma" not in page
    # The filter keeps the release a search by hand is for.
    page = client.get(f"/get/find?q=x&artist=Yorushika&album=Elma&w={wid}").text
    assert f"/get/find?show=other&q=x&artist=Yorushika&album=Elma&w={wid}" in page.replace(
        "&amp;", "&"
    )
    # Only searches of the other kind: the page says so, with a way to them.
    found[:] = [("s2", "lofi beats")]
    page = client.get("/get/find").text
    assert "No searches for a release yet." in page and "Show the 1 other search" in page


# ------------------------------------------------------------- in bulk


def test_discover_gets_a_whole_list(client, app):
    state = app.state.state
    fresh = [
        {
            "id": f"{i}e2d1f4c-7c5b-4a51-9d3e-2f6f0a1b2c3d",
            "release_id": "",
            "title": f"Fresh {i}",
            "artist": "Someone",
            "date": "2020-01-0" + str(i),
            "type": t,
            "kind": t.lower(),
            "secondary": "",
        }
        for i, t in ((1, "Album"), (2, "Album"), (3, "Single"))
    ]
    state.set_meta(
        discover.META_KEY,
        {"at": time.time(), "user": "me", "playlists": [], "albums": [], "fresh": fresh},
    )
    app.state.settings.listenbrainz_user = "me"
    page = client.get("/get/discover").text
    assert "Get 2 new releases" in page  # the singles aren't shown, so not got either
    r = client.post("/wanted/discover-all", data={"section": "released", "kinds": ["album"]})
    assert r.status_code == 200
    assert sorted(w["album"] for w in state.wanted_list()) == ["Fresh 1", "Fresh 2"]
    # Asked for already: no button, and asking again adds nothing.
    assert "Get 2 new releases" not in client.get("/get/discover").text
    client.post("/wanted/discover-all", data={"section": "released", "kinds": ["album"]})
    assert len(state.wanted_list()) == 2


def test_search_again_for_every_failed_request(client, app):
    state = app.state.state
    ids = [
        want(state, status="failed", status_text="Nothing found", release_id=str(i))
        for i in range(2)
    ]
    other = want(state, status="choosing", release_id="x")
    assert "Search again for all 2" in client.get("/get/coming").text
    r = client.post("/wanted/retry-all", headers=FRAGMENT)
    assert "Searching again for 2 albums" in r.headers["HX-Trigger"]
    assert [state.wanted(i)["status"] for i in ids] == ["queued", "queued"]
    assert state.wanted(other)["status"] == "choosing"

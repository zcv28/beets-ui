"""Get, around the release: searches and downloads belong to the release they were for,
the engine keeps trying before it asks, and Missing shows covers. Also the Library
artwork tabs and the console on the jobs page."""

# The Get tests' fixtures are imported below, which pytest needs and ruff reads as redefinitions.
# ruff: noqa: F811

from __future__ import annotations

import time

import httpx
import pytest
import respx

from beets_ui import acquire
from beets_ui.release_covers import ReleaseCovers, path_for
from beets_ui.sources import SourceError

# The fakes and fixtures of the Get tests (pytest finds fixtures imported here).
from test_get import FakeClient, FakeSource, app, client, engine, offer, transfer, want  # noqa: F401

PNG = b"\x89PNG\r\n\x1a\n" + bytes(32)


class SearchingSource(FakeSource):
    """Remembers how long each search waits, lists its searches, and lets an uploader
    not answer for a listing."""

    def __init__(self):
        super().__init__()
        self.waits: list[int] = []
        self.silent: set[str] = set()

    def search(self, text, **opts):
        self.waits.append(opts.get("wait"))
        return super().search(text, **opts)

    def searches(self):
        return [
            {
                "id": f"s{i}",
                "text": t,
                "running": False,
                "label": "Finished",
                "answers": 2,
                "files": 9,
            }
            for i, t in reversed(list(enumerate(self.searched, 1)))
        ]

    def listing(self, origin, path):
        if origin in self.silent:
            raise SourceError(f"{origin} sent no folder listing")
        return super().listing(origin, path)


@pytest.fixture
def src(app, engine):
    """Used after ``client``: the app and the engine both get this source."""
    acq, _ = engine
    s = SearchingSource()
    acq.source = s
    acq.transfers = s.transfers
    app.state.source = s
    return s


# ------------------------------------------------------------- the engine


def test_queries_drop_brackets_then_try_the_album_alone():
    w = {
        "artist": "Some Circle",
        "album": "Hidden Garden Stories (Remastered)",
        "artist_original": "",
        "album_original": "",
    }
    assert acquire.queries(w) == [
        "Some Circle Hidden Garden Stories Remastered",
        "Some Circle Hidden Garden Stories",
        "Hidden Garden Stories",
    ]
    # A short, common name is never searched for on its own.
    assert acquire.queries({**w, "album": "Best (Disc 1)"}) == [
        "Some Circle Best Disc 1",
        "Some Circle Best",
    ]


def test_each_round_of_searches_waits_longer(app, engine, src):
    acq, _ = engine
    wid = want(app.state.state)
    for _ in range(8):
        acq.tick()
    w = app.state.state.wanted(wid)
    assert w["status"] == "failed"
    assert src.waits == [30, 60, 90]
    texts = [h["text"] for h in w["history"]]
    assert texts[0].startswith("Searched for “Test Artist Test Album”: 1 answer, 0 folders")
    assert "Nothing found: searching again (round 2)" in texts
    # Every search is remembered with the request it was for.
    assert [s["text"] for s in app.state.state.searches_for_wanted(wid)] == [
        "Test Artist Test Album"
    ] * 3


def test_an_uploader_who_answers_with_the_listing_goes_first(app, engine, src):
    acq, _ = engine
    wid = want(app.state.state)
    quiet = offer("alice", "Test Artist - Test Album")
    answers = offer("carol", "Test Artist - Test Album [FLAC]")
    src.offered = [quiet, answers]
    src.silent = {"alice"}
    src.listings[(answers["origin"], answers["path"])] = answers["files"]
    acq.tick()
    acq.tick()
    w = app.state.state.wanted(wid)
    assert w["status"] == "downloading" and w["origin"] == "carol"
    assert w["history"][-1]["text"] == "Downloading “Test Artist - Test Album [FLAC]” from carol"


def test_a_silent_uploader_is_still_tried_when_nobody_else_has_it(app, engine, src):
    acq, _ = engine
    wid = want(app.state.state)
    src.offered = [offer("alice", "Test Artist - Test Album")]
    src.silent = {"alice"}
    acq.tick()
    acq.tick()
    assert app.state.state.wanted(wid)["origin"] == "alice"  # the files the search showed


def test_keep_trying_starts_over_and_says_so(client, app, src):
    state = app.state.state
    wid = want(state, status="failed", status_text="Nothing found after 3 rounds of searches")
    state.update_wanted(wid, researched=2, attempts=6, query_idx=1)
    page = client.get("/get/coming").text
    assert "Needs you" in page and "Nothing found after 3 rounds of searches" in page
    for choice in ("Search again", "Find by hand", "Import by hand"):
        assert choice in page, choice
    assert f"&amp;w={wid}" in page  # Find by hand is for this request
    client.post(f"/wanted/{wid}/retry")
    w = state.wanted(wid)
    assert (w["status"], w["researched"], w["attempts"], w["query_idx"]) == ("queued", 0, 0, 0)
    assert w["history"][-1]["text"] == "You asked it to keep trying"


# ------------------------------------------------- Find, for a release


def test_a_download_by_hand_for_a_request_goes_to_it(client, app, engine, src):
    state = app.state.state
    wid = want(state, status="failed", status_text="Nothing found")
    page = client.get(f"/get/find?q=test+album&artist=Test+Artist&album=Test+Album&w={wid}").text
    assert "Finding" in page and "goes to its request" in page
    r = client.post(
        "/get/find/search",
        data={"q": "test album", "artist": "Test Artist", "album": "Test Album", "w": wid},
        follow_redirects=False,
    )
    sid = r.headers["location"].rsplit("/", 1)[1]
    assert state.searches_logged([sid])[sid]["wanted_id"] == wid
    o = offer("dave", "Test Album [FLAC]")
    src.offered = [o]
    src.listings[(o["origin"], o["path"])] = o["files"]
    client.post(
        "/get/find/download",
        data={"search_id": sid, "username": "dave", "folder": o["folder"]},
    )
    w = state.wanted(wid)
    assert (
        w["status"] == "downloading"
        and w["origin"] == "dave"
        and w["folder"] == "Test Album [FLAC]"
    )
    assert "under Find" in w["history"][-1]["text"]
    # Not a loose download of its own as well.
    assert not (state.meta(acquire.FETCHES_KEY) or {})


def test_find_groups_searches_by_release(client, app, src):
    state = app.state.state
    wid = want(state, album="Elma", artist="Yorushika")
    for text, kw in (
        ("yorushika elma", {"wanted_id": wid, "artist": "Yorushika", "album": "Elma"}),
        ("ヨルシカ エルマ", {"wanted_id": wid, "artist": "Yorushika", "album": "Elma"}),
        ("some circle", {"artist": "Some Circle", "album": "Garden"}),
        ("random words", {}),
    ):
        sid = src.search(text)
        if kw:
            state.log_search(
                sid, text, kw.get("artist", ""), kw.get("album", ""), kw.get("wanted_id")
            )
    from beets_ui.routes.find import group_searches

    groups = group_searches(state, src.searches())
    assert [(g["title"], len(g["searches"])) for g in groups] == [
        ("random words", 1),
        ("Garden", 1),
        ("Elma", 2),
    ]
    page = client.get("/get/find").text
    assert "searched 2 times" in page and "a search for no release in particular" in page


def test_coming_in_is_one_list_of_releases(client, app, engine, src):
    acq, _ = engine
    state = app.state.state
    wid = want(state)
    good = offer("john", "Test Artist - Test Album")
    src.offered = [good]
    acq.tick()
    acq.tick()
    row = transfer(good, "downloading")
    row.update(folder_name=good["folder_name"], speed=2048, size=3000, files=[])
    from beets_ui.sources import Watcher

    app.state.source_watch = Watcher(src)
    src.rows = [row]
    app.state.source_watch.poll()
    page = client.get("/get/coming").text
    assert 'id="coming-list"' in page and f'id="c-w{wid}"' in page
    assert 'Downloading from <span class="strong">john</span>' in page and "50%" in page
    assert page.count('class="release"') == 1  # its download isn't listed twice


# ----------------------------------------------------------- covers


@respx.mock
def test_release_covers_are_fetched_once_and_misses_remembered(tmp_path):
    caa = respx.get(url__startswith="https://coverartarchive.org/").mock(
        return_value=httpx.Response(200, content=PNG)
    )
    covers = ReleaseCovers(tmp_path, httpx.Client(follow_redirects=True))
    rg = "1b022e01-4da6-387b-8658-8678046e4cef"
    path, kind = covers.get("musicbrainz", rg)
    assert kind == "image/png" and path.read_bytes() == PNG
    covers.get("musicbrainz", rg)
    assert caa.call_count == 1
    voca = respx.get("https://vocadb.net/Album/CoverPictureThumb/42").mock(
        return_value=httpx.Response(404)
    )
    assert covers.get("vocadb", "42") is None
    assert covers.get("vocadb", "42") is None
    assert voca.call_count == 1
    # VocaDB answers an album without a picture with its own placeholder, not a redirect.
    respx.get("https://vocadb.net/Album/CoverPictureThumb/43").mock(
        return_value=httpx.Response(200, content=PNG)
    )
    assert covers.get("vocadb", "43") is None
    respx.get("https://vocadb.net/Album/CoverPictureThumb/44").mock(
        return_value=httpx.Response(
            302, headers={"Location": "https://static.vocadb.net/img/44.png"}
        )
    )
    respx.get("https://static.vocadb.net/img/44.png").mock(
        return_value=httpx.Response(200, content=PNG)
    )
    assert covers.get("vocadb", "44")[1] == "image/png"
    # Nothing that isn't a known source and ID is ever fetched.
    assert covers.get("vocadb", "../etc") is None and path_for("musicbrainz", "nope") is None
    assert path_for("touhoudb", "7") == "/get/cover/touhoudb/7"


def test_missing_shows_release_covers(client, app):
    from beets_ui import beets_ctx, importer

    from conftest import make_album

    folder = make_album(app.state.settings.inbox / "seed")
    key = importer.preview_folder(str(folder))[0]["key"]
    importer.apply_folder(str(folder), {key: {"action": "apply", "id": "fake-1", "source": "Fake"}})
    a = beets_ctx.lib().albums()[0]
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
        "catalog": "ABC-1",
        "artists": "",
        "url": "",
        "names": ["New EP"],
    }
    soon = {**rel, "id": "501", "title": "Next One", "date": "2099-01-01", "names": ["Next One"]}
    app.state.state.set_discography("vocadb", "77", "Test Artist", [rel, soon])
    page = client.get("/get/missing").text
    # An unreleased album can't be got yet.
    assert page.count('name="release_id" value="501"') == 1  # only its Dismiss form
    assert "Not out yet" in page
    assert 'src="/get/cover/vocadb/500"' in page and "ABC-1" in page
    assert "&amp;artist=Test%20Artist&amp;album=New%20EP" in page


# --------------------------------------------------- library and jobs


def test_artwork_lives_under_library(client):
    r = client.get("/artwork?filter=low", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/library/covers?filter=low"
    assert (
        client.get("/artwork/artists", follow_redirects=False).headers["location"]
        == "/library/pictures"
    )
    page = client.get("/library/covers").text
    assert 'href="/library/covers" aria-current="page"' in page
    assert 'href="/artwork"' not in page  # no Artwork section in the sidebar


def test_console_is_a_drop_down_on_the_jobs_page(client, app):
    r = client.get("/console?cmd=ls", follow_redirects=False)
    assert r.headers["location"] == "/jobs?console=1&cmd=ls"
    page = client.get("/jobs?console=1&cmd=ls").text
    assert '<details class="console-drop" id="console" open>' in page and 'value="ls"' in page
    assert 'href="/console"' not in page
    job = app.state.jobs.submit("console", "beet version", {"argv": ["version"]}, None)
    page = client.get(f"/jobs/{job}").text
    # The list and the chosen job side by side; the console stays closed.
    assert 'class="jobs-split has-job"' in page and 'aria-current="true"' in page
    assert (
        "Waiting for the job to start" in page
        and '<details class="console-drop" id="console">' in page
    )
    assert f"/jobs/table?sel={job}" in page


# ------------------------------------------------- needs you, one definition


def test_needs_you_is_the_same_on_coming_in_and_home(client, app, src):
    state = app.state.state
    want(state, album="Pick Me", status="choosing", status_text="No copy was clearly right")
    want(state, album="Lost One", status="failed", status_text="Nothing found", release_id="x2")
    want(
        state, album="In Review", status="review", status_text="In the inbox: 80%", release_id="x3"
    )
    want(state, album="Moving", status="searching", status_text="Searching for it", release_id="x4")
    page = client.get("/get/coming").text
    needs, _, rest = page.partition('id="h-on-its-way"')
    assert 'Needs you <span class="count">2</span>' in needs
    assert "Pick Me" in needs and "Lost One" in needs and "In Review" not in needs
    assert "In Review" in rest and "Moving" in rest
    # One main action per stopped row, beside it.
    assert needs.count('class="release-quick"') == 2 and ">Search again</button>" in needs
    home = client.get("/home/now").text
    assert "Pick a copy of Pick Me" in home and "Couldn&#39;t get Lost One" in home
    assert "In Review" in home.partition('id="h-coming"')[2]


def test_history_folds_repeats_and_says_when():
    from beets_ui.web import fmt_ago, history_view

    now = time.time()
    h = [
        {"at": now - 7200, "text": "Searched"},
        {"at": now - 60, "text": "Nothing found"},
        {"at": now - 30, "text": "Nothing found"},
    ]
    assert [(x["text"], x["n"]) for x in history_view(h)] == [("Nothing found", 2), ("Searched", 1)]
    assert ">2 h ago</time>" in fmt_ago(now - 7200) and 'datetime="' in fmt_ago(now)


def test_a_stop_with_one_copy_says_copy(app, engine, src):
    acq, _ = engine
    wid = want(app.state.state)
    src.offered = [offer("bob", "Test Album", tracks=2)]
    for _ in range(8):
        acq.tick()
    w = app.state.state.wanted(wid)
    assert (
        w["history"][-1]["text"] == "Stopped after 3 rounds of searches: 1 copy waits for your pick"
    )
    assert any("(round 2)" in h["text"] for h in w["history"])
    searches = app.state.state.searches_for_wanted(wid)
    assert searches and all(s["answers"] == 1 for s in searches)

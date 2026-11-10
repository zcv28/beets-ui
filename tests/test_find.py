"""Find music and downloads: full folder listings, file choice, retry, search options,
connection status and pop-ups, through the Soulseek source."""

from __future__ import annotations

import json
import re
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from beets_ui.settings import Settings
from beets_ui.slskd import Slskd, SlskdError, _file, flatten_downloads
from beets_ui.sources import Watcher
from beets_ui.sources.soulseek import SoulseekSource

BASE = "http://slskd:5030/api/v0"
FOLDER = "Music\\Artist\\Album (2020)"

RESPONSES = [
    {
        "username": "alice",
        "hasFreeUploadSlot": True,
        "queueLength": 0,
        "uploadSpeed": 500000,
        "files": [
            {"filename": f"{FOLDER}\\01 a.flac", "size": 1000, "extension": "flac", "length": 200},
            {"filename": f"{FOLDER}\\02 b.flac", "size": 2000, "extension": "flac", "length": 180},
        ],
    }
]

# A browse listing names files without their folder, and holds files the search missed.
LISTING = {
    "name": FOLDER,
    "files": [
        {"filename": "01 a.flac", "size": 1000, "extension": "flac"},
        {"filename": "02 b.flac", "size": 2000, "extension": "flac"},
        {"filename": "03 c.flac", "size": 3000, "extension": "flac"},
        {"filename": "cover.jpg", "size": 50, "extension": "jpg"},
    ],
}


def transfer(fid, name, state, size=100, done=0, **extra):
    return {
        "id": fid,
        "filename": f"{FOLDER}\\{name}",
        "size": size,
        "bytesTransferred": done,
        "state": state,
        **extra,
    }


def downloads(*files):
    return [{"username": "alice", "directories": [{"directory": FOLDER, "files": list(files)}]}]


# ------------------------------------------------------------------ client


@respx.mock
def test_directory_joins_folder_and_names():
    route = respx.post(f"{BASE}/users/alice/directory").mock(
        return_value=httpx.Response(200, json=LISTING)
    )
    files = Slskd("http://slskd:5030", "key").directory("alice", FOLDER)
    assert json.loads(route.calls[0].request.content) == {"directory": FOLDER}
    assert [f["filename"] for f in files][:3] == [
        f"{FOLDER}\\01 a.flac",
        f"{FOLDER}\\02 b.flac",
        f"{FOLDER}\\03 c.flac",
    ]
    assert {f["name"]: f["audio"] for f in files}["cover.jpg"] is False


@respx.mock
def test_directory_accepts_a_list_and_quotes_the_user():
    respx.post(f"{BASE}/users/dj%20x/directory").mock(
        return_value=httpx.Response(200, json=[{"name": "other", "files": []}, LISTING])
    )
    files = Slskd("http://slskd:5030", "key").directory("dj x", FOLDER)
    assert len(files) == 4


@respx.mock
def test_search_options_and_stop():
    search = respx.post(f"{BASE}/searches").mock(return_value=httpx.Response(200, json={}))
    c = Slskd("http://slskd:5030", "key")
    sid = c.search("artist album", wait=60, max_queue=10, min_speed=500_000)
    body = json.loads(search.calls[0].request.content)
    assert body["searchTimeout"] == 60_000
    assert body["maximumPeerQueueLength"] == 10
    assert body["minimumPeerUploadSpeed"] == 500_000
    c.search("plain search")
    assert "maximumPeerQueueLength" not in json.loads(search.calls[1].request.content)
    stop = respx.put(f"{BASE}/searches/{sid}").mock(return_value=httpx.Response(200))
    c.stop_search(sid)
    assert stop.called


def test_flatten_states_and_labels():
    rows = flatten_downloads(
        downloads(
            transfer("1", "01.flac", "Completed, Succeeded", done=100),
            transfer("2", "02.flac", "Completed, Errored"),
            transfer("3", "03.flac", "Completed, TimedOut"),
        )
    )
    row = rows[0]
    assert row["state"] == "failed" and row["label"] == "2 of 3 failed"
    assert [f["failure"] for f in row["failed_files"]] == ["Error", "Timed out"]

    rows = flatten_downloads(downloads(transfer("1", "01.flac", "Completed, Cancelled")))
    assert rows[0]["state"] == "cancelled"

    rows = flatten_downloads(
        downloads(
            transfer("1", "01.flac", "Queued, Remotely", placeInQueue=7),
            transfer("2", "02.flac", "Queued, Remotely", placeInQueue=4),
        )
    )
    assert rows[0]["state"] == "queued" and rows[0]["place"] == 4

    rows = flatten_downloads(
        downloads(transfer("1", "01.flac", "InProgress", done=50, averageSpeed=2048))
    )
    assert rows[0]["state"] == "downloading" and rows[0]["speed"] == 2048


# ----------------------------------------------------------------- watcher


class FakeSlskd:
    """Stands in for the slskd client in route and watcher tests."""

    def __init__(self):
        self.connected = True
        self.reachable = True
        self.listing: list | Exception = []
        self.transfers: list = []
        self.enqueued: list = []
        self.cancelled: list = []
        self.searched: list = []
        self.stopped: list = []

    def server(self):
        if not self.reachable:
            raise SlskdError("Can't reach slskd: connection refused")
        return {"isConnected": self.connected, "isLoggedIn": self.connected, "username": "me"}

    def connect(self):
        self.connected = True

    def search(self, text, **opts):
        self.searched.append((text, opts))
        return "s1"

    def search_state(self, sid):
        return {"id": sid, "searchText": "artist album", "state": "Completed, TimedOut"}

    def search_responses(self, sid):
        return RESPONSES

    def searches(self):
        return [{"id": "s1", "searchText": "artist album", "state": "InProgress"}]

    def stop_search(self, sid):
        self.stopped.append(sid)

    def delete_search(self, sid):
        pass

    def directory(self, username, path):
        if isinstance(self.listing, Exception):
            raise self.listing
        return self.listing

    def enqueue(self, username, files):
        self.enqueued.append((username, [f["filename"] for f in files]))

    def downloads(self):
        return self.transfers

    def cancel(self, username, tid, remove=False):
        self.cancelled.append((tid, remove))

    def clear_completed(self):
        pass


def test_watcher_reports_finished_downloads_once():
    fake = FakeSlskd()
    w = Watcher(SoulseekSource(fake))
    fake.transfers = downloads(transfer("1", "01.flac", "InProgress"))
    w.poll()
    assert w.status["connected"] and not w.down and not w.events
    fake.transfers = downloads(transfer("1", "01.flac", "Completed, Succeeded", done=100))
    w.poll()
    w.poll()
    assert [e["ok"] for e in w.events] == [True]
    assert w.events_since(time.time() - 60) and not w.events_since(time.time() + 1)


def test_watcher_marks_slskd_down():
    fake = FakeSlskd()
    w = Watcher(SoulseekSource(fake))
    fake.connected = False
    w.poll()
    assert w.down and w.status["reachable"]
    fake.reachable = False
    w.poll()
    assert w.down and not w.status["reachable"]


# ------------------------------------------------------------------ routes


@pytest.fixture
def slsk(env, fake_source, monkeypatch):
    from beets_ui.main import create_app

    monkeypatch.setenv("SLSKD_URL", "http://slskd:5030")
    monkeypatch.setenv("SLSKD_API_KEY", "key")
    app = create_app(Settings.from_env(), background=False)
    fake = FakeSlskd()
    app.state.source = SoulseekSource(fake)
    app.state.source_watch = Watcher(app.state.source)
    c = TestClient(app)
    assert c.post("/login", data={"password": "secret"}, follow_redirects=False).status_code == 303
    return c, fake, app


def test_folder_page_lists_the_full_folder(slsk):
    c, fake, _ = slsk
    fake.listing = [_file(f, f"{FOLDER}\\{f['filename']}") for f in LISTING["files"]]
    r = c.get("/inbox/find/s/s1/results")
    assert "/inbox/find/s/s1/folder?u=alice&amp;f=Music%2FArtist%2FAlbum%20%282020%29" in r.text
    page = c.get("/inbox/find/s/s1/folder", params={"u": "alice", "f": "Music/Artist/Album (2020)"})
    assert page.status_code == 200 and "Asking alice for the full folder" in page.text
    files = c.get(
        "/inbox/find/s/s1/folder/files", params={"u": "alice", "f": "Music/Artist/Album (2020)"}
    )
    assert "03 c.flac" in files.text and "cover.jpg" in files.text
    assert "2 more files than the search showed" in files.text
    assert "4 of 4 files" in files.text


def test_folder_page_falls_back_to_matched_files(slsk):
    c, fake, _ = slsk
    fake.listing = SlskdError("timed out")
    r = c.get(
        "/inbox/find/s/s1/folder/files", params={"u": "alice", "f": "Music/Artist/Album (2020)"}
    )
    assert "didn" in r.text and "full folder" in r.text and "2 of 2 files" in r.text


def test_download_whole_folder_uses_the_listing(slsk):
    c, fake, _ = slsk
    fake.listing = [_file(f, f"{FOLDER}\\{f['filename']}") for f in LISTING["files"]]
    r = c.post(
        "/inbox/find/download",
        data={"search_id": "s1", "username": "alice", "folder": "Music/Artist/Album (2020)"},
    )
    assert r.status_code == 200 and "Downloading 4 files from alice" in r.text
    assert len(fake.enqueued[0][1]) == 4


def test_download_falls_back_and_says_so(slsk):
    c, fake, _ = slsk
    fake.listing = SlskdError("offline")
    r = c.post(
        "/inbox/find/download",
        data={"search_id": "s1", "username": "alice", "folder": "Music/Artist/Album (2020)"},
    )
    assert "Downloading 2 files" in r.text and "only the files that matched" in r.text


def test_download_chosen_files(slsk):
    c, fake, _ = slsk
    r = c.post(
        "/inbox/find/download",
        data={
            "search_id": "s1",
            "username": "alice",
            "folder": "Music/Artist/Album (2020)",
            "file": [f"3000:{FOLDER}\\03 c.flac", "50:" + FOLDER + "\\cover.jpg"],
        },
    )
    assert "Downloading 2 files" in r.text
    assert fake.enqueued == [("alice", [f"{FOLDER}\\03 c.flac", f"{FOLDER}\\cover.jpg"])]


def test_retry_requeues_and_drops_old_entries(slsk):
    c, fake, _ = slsk
    fake.transfers = downloads(
        transfer("1", "01.flac", "Completed, Succeeded", done=100),
        transfer("2", "02.flac", "Completed, Errored"),
    )
    app = c.app
    app.state.source_watch.poll()
    # The inbox lists the download that is still not done, with Retry.
    page = c.get("/inbox")
    assert 'id="downloads"' in page.text
    assert "1 of 2 failed" in page.text and "Retry" in page.text
    r = c.post(
        "/inbox/downloads/retry",
        data={
            "username": "alice",
            "name": "Album (2020)",
            "file": [f"100:{FOLDER}\\02.flac"],
            "id": ["2"],
        },
        headers={"HX-Request": "true"},
    )
    assert json.loads(r.headers["HX-Trigger"])["toast"]["title"] == "Retrying Album (2020)"
    assert fake.enqueued == [("alice", [f"{FOLDER}\\02.flac"])]
    assert fake.cancelled == [("2", True)]


def test_search_options_are_remembered(slsk):
    c, fake, _ = slsk
    c.post(
        "/inbox/find/search",
        data={"q": "artist album", "wait": "60", "max_queue": "10", "min_speed": "999"},
    )
    assert fake.searched[-1][1] == {"wait": 60, "max_queue": 10, "min_speed": 0}
    # "Search again" sends only the text: the saved options apply.
    c.post("/inbox/find/search", data={"q": "other album"})
    assert fake.searched[-1][1]["wait"] == 60
    assert 'value="60" selected' in c.get("/inbox/find").text


def test_stop_search_and_recent_list(slsk):
    c, fake, _ = slsk
    page = c.get("/inbox/find")
    assert "Searching" in page.text and "Delete search artist album" in page.text
    c.post("/inbox/find/s/s1/stop")
    assert fake.stopped == ["s1"]


def test_disconnected_slskd_shows_everywhere(slsk):
    c, fake, app = slsk
    fake.connected = False
    app.state.source_watch.poll()
    assert 'class="nav-alert"' in c.get("/inbox").text
    page = c.get("/inbox/find")
    assert "Downloads are paused" in page.text and "Reconnect" in page.text
    c.post("/inbox/sources/connect", data={"back": "/inbox"})
    assert fake.connected and not app.state.source_watch.down
    assert 'class="nav-alert"' not in c.get("/inbox").text


def test_finished_download_pops_up(slsk):
    c, fake, app = slsk
    since = time.time()
    fake.transfers = downloads(transfer("1", "01.flac", "InProgress"))
    app.state.source_watch.poll()
    fake.transfers = downloads(transfer("1", "01.flac", "Completed, Succeeded", done=100))
    app.state.source_watch.poll()
    r = c.get(f"/jobs/pill?since={since}")
    assert "Downloaded" in r.text and "Album (2020)" in r.text and "Open inbox" in r.text
    # The next poll carries the newer timestamp and shows nothing again.
    nxt = re.search(r"since=([0-9.]+)", r.text).group(1)
    assert "Downloaded" not in c.get(f"/jobs/pill?since={nxt}").text


def test_finished_downloads_leave_the_list(slsk):
    c, fake, app = slsk
    fake.transfers = downloads(transfer("1", "01.flac", "Completed, Succeeded", done=100))
    app.state.source_watch.poll()
    assert 'id="downloads"' in c.get("/inbox").text
    assert "Album (2020)" not in c.get("/inbox/downloads").text


def test_pages_do_not_name_the_source(slsk):
    c, fake, app = slsk
    fake.transfers = downloads(transfer("1", "01.flac", "InProgress"))
    app.state.source_watch.poll()
    for url in ("/inbox", "/inbox/find", "/inbox/find/s/s1", "/inbox/downloads"):
        assert "Soulseek" not in c.get(url).text, url
    sidebar = c.get("/library").text
    assert 'href="/soulseek"' not in sidebar
    # The folder page says where it comes from, once.
    folder = c.get(
        "/inbox/find/s/s1/folder", params={"u": "alice", "f": "Music/Artist/Album (2020)"}
    )
    assert folder.text.count("Soulseek") == 1


def test_old_addresses_redirect(slsk):
    c, _, _ = slsk
    r = c.get("/soulseek?q=yorushika%20elma", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/inbox/find?q=yorushika%20elma"
    r = c.get("/soulseek/s/s1", follow_redirects=False)
    assert r.headers["location"] == "/inbox/find/s/s1"
    assert c.get("/soulseek/transfers", follow_redirects=False).headers["location"] == "/inbox"

"""Discography gaps, upgrades, listening stats, synced lyrics, job lanes."""

from __future__ import annotations

import os
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from mediafile import MediaFile

from beets_ui import beets_ctx, discography, listening, lyrics, upgrades
from beets_ui.settings import Settings

from conftest import make_album
from test_pages import Checker


def balanced(html: str) -> None:
    chk = Checker()
    chk.feed(html)
    assert not chk.errors and chk.stack == [], (chk.errors[:5], chk.stack)


@pytest.fixture
def app(env, monkeypatch):
    from beets_ui.main import create_app

    monkeypatch.setenv("JELLYFIN_URL", "http://jellyfin:8096")
    monkeypatch.setenv("JELLYFIN_API_KEY", "key")
    return create_app(Settings.from_env(), background=False)


@pytest.fixture
def client(app):
    c = TestClient(app)
    c.post("/login", data={"password": "secret"})
    return c


def add_album(env, artist="Circle", album="Album", n=3, **fields):
    """An album in the library directory, added straight to beets."""
    from beets.library import Item

    folder = make_album(env["music"] / artist / album, artist=artist, album=album, n=n)
    lib = beets_ctx.lib()
    items = [Item.from_path(str(p)) for p in sorted(folder.glob("*.mp3"))]
    a = lib.add_album(items)
    for k, v in fields.items():
        a[k] = v
    a.store(inherit=True)
    return a


# ----------------------------------------------------------------- lyrics

LRC = (
    "[ar:ヨルシカ]\n[00:01.00]考えたってわからないし\n[00:05.50]\n[00:07.25]青空の下、君を待った\n"
)


def test_lrc_parsing_and_romaji_layouts():
    lines = lyrics.parse_lrc(LRC)
    assert lines == [(1000, "考えたってわからないし"), (5500, ""), (7250, "青空の下、君を待った")]
    assert lyrics.is_synced(LRC) and not lyrics.is_synced("just words")

    text, how = lyrics.add_romaji(LRC, "below", lyrics.Romanizer())
    assert how == "cutlet"
    assert "[00:07.25]青空の下、君を待った\n[00:07.25]Aozora no" in text
    assert "kimi wo matta" in text
    rows = lyrics.grouped(text)
    assert rows[0]["text"] == "考えたってわからないし"
    assert rows[0]["sub"] and rows[0]["sub"][0].startswith("Kangae")
    assert rows[1] == {"ms": 5500, "time": "00:05.50", "text": "", "sub": []}

    # VocaDB's own romaji wins when it has one line per sung line.
    text, how = lyrics.add_romaji(LRC, "only", lyrics.Romanizer(), ["kangaetatte", "aozora"])
    assert how == "vocadb"
    assert text == "[00:01.00]kangaetatte\n[00:05.50]\n[00:07.25]aozora\n"

    text, _ = lyrics.add_romaji(LRC, "inline", lyrics.Romanizer())
    assert text.splitlines()[0].startswith("[00:01.00]考えたってわからないし / Kangae")

    # Not Japanese, or turned off: unchanged lines.
    english = "[00:01.00]hello there\n"
    assert lyrics.add_romaji(english, "below", lyrics.Romanizer()) == (english, "")
    assert lyrics.add_romaji(LRC, "off", lyrics.Romanizer())[0].count("[00:") == 3


@respx.mock
def test_lyrics_job_prefers_original_names_writes_tags_and_undoes(app, client, env):
    a = add_album(env, artist="Yorushika", album="Dakara", n=2)
    first, second = sorted(a.items(), key=lambda i: i.track)
    first.title_original = "だから僕は音楽を辞めた"
    first.artist_original = "ヨルシカ"
    first.store()

    def lrclib(request):
        if request.url.params.get("track_name") == "だから僕は音楽を辞めた":
            assert request.url.params["artist_name"] == "ヨルシカ"
            return httpx.Response(200, json={"syncedLyrics": LRC, "instrumental": False})
        return httpx.Response(404, json={})

    respx.get("https://lrclib.net/api/get").mock(side_effect=lrclib)
    respx.get("https://lrclib.net/api/search").mock(return_value=httpx.Response(200, json=[]))

    r = client.post(f"/library/album/{a.id}/lyrics", follow_redirects=False)
    job_id = int(r.headers["location"].rsplit("/", 1)[1])
    app.state.jobs.run_pending_sync()
    job = app.state.state.job(job_id)
    assert job["status"] == "done", job["log"]
    assert "Done: synced lyrics for 1 of 2" in job["log"]

    first.load()
    assert lyrics.is_synced(first.lyrics) and "kimi wo matta" in first.lyrics
    assert first.lyrics_backend == "lrclib" and first.lyrics_romaji == "cutlet"
    assert "Aozora" in MediaFile(os.fsdecode(first.path)).lyrics
    second.load()
    assert not second.lyrics and second.get("lyrics_checked")

    page = client.get(f"/library/item/{first.id}")
    balanced(page.text)
    assert "00:07.25" in page.text and "kimi wo matta" in page.text
    assert "Has synced lyrics" in client.get(f"/library/album/{a.id}").text
    assert "1/2</span> synced" in client.get("/insights/lyrics").text

    # A bulk run skips the track checked a moment ago and the one already synced.
    client.post("/insights/lyrics/run", data={"replace_plain": "1"})
    app.state.jobs.run_pending_sync()
    bulk = app.state.state.jobs(1, kind="lyrics")[0]["id"]
    assert "for 0 track(s)" in app.state.state.job(bulk)["log"]

    client.post(f"/jobs/{job_id}/undo")
    app.state.jobs.run_pending_sync()
    first.load()
    assert not first.lyrics and "lyrics_backend" not in first


# --------------------------------------------------------------- upgrades


def test_upgrades_lists_lossy_low_bitrate_and_incomplete(app, client, env):
    a = add_album(env, album="Lossy")  # the test MP3s are 128 kbps
    b = add_album(env, album="Short", n=2)
    for it in b.items():
        it.tracktotal = 4
        it.store()
    data = upgrades.find(beets_ctx.lib(), app.state.state)
    by_id = {r["id"]: r for r in data["rows"]}
    assert by_id[a.id]["issues"]["bitrate"] == "MP3 128 kbps"
    assert "lossy" in by_id[a.id]["issues"] and "incomplete" not in by_id[a.id]["issues"]
    assert by_id[b.id]["issues"]["incomplete"] == "2 tracks missing"
    assert data["counts"]["incomplete"] == 1

    r = client.post(
        "/insights/upgrades/dismiss",
        data={"album_id": b.id, "reason": "incomplete"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and r.text == ""
    balanced(client.get("/insights/upgrades").text)
    page = client.get("/insights/upgrades?reason=incomplete")
    assert "Nothing to upgrade" in page.text and "1 dismissed" in page.text
    client.post("/insights/upgrades/restore")
    assert "Short" in client.get("/insights/upgrades?reason=incomplete").text


# ------------------------------------------------------------ discography


def touhoudb_album(i, name, original="", disc="Album", date=(2020, 1, 1)):
    return {
        "id": i,
        "name": name,
        "defaultName": original or name,
        "additionalNames": "",
        "discType": disc,
        "releaseDate": {"isEmpty": False, "year": date[0], "month": date[1], "day": date[2]},
        "catalogNumber": f"CAT-{i}",
        "artistString": "Circle",
    }


@respx.mock
def test_discography_gaps_by_id_and_title(app, client, env):
    add_album(
        env,
        album="Owned By Id",
        data_source="TouhouDB",
        mb_albumartistid="77",
        mb_albumid="1",
    )
    add_album(env, album="Owned By Title", data_source="TouhouDB", mb_albumartistid="77")
    add_album(env, artist="MB Band", album="Debut", mb_albumartistid="", mb_albumid="")
    route = respx.get("https://touhoudb.com/api/albums").mock(
        return_value=httpx.Response(
            200,
            json={
                "totalCount": 5,
                "items": [
                    touhoudb_album(1, "Owned By Id (renamed)"),
                    touhoudb_album(2, "Owned by title!"),
                    touhoudb_album(3, "Missing One", "欠けた一枚"),
                    touhoudb_album(4, "A Single", disc="Single"),
                    touhoudb_album(5, "Future", date=(2999, 1, 1)),
                ],
            },
        )
    )
    client.post("/get/missing/refresh")
    app.state.jobs.run_pending_sync()
    assert route.calls[0].request.url.params["artistId[]"] == "77"
    job = app.state.state.jobs(1, kind="discography")[0]
    assert job["status"] == "done", job["log"]

    [row] = discography.gaps(beets_ctx.lib(), app.state.state)
    assert [r["title"] for r in row["owned"]] == ["Owned By Id (renamed)", "Owned by title!"]
    assert [r["title"] for r in row["missing"]] == ["Missing One"]
    assert [r["title"] for r in row["upcoming"]] == ["Future"]
    with_singles = discography.gaps(beets_ctx.lib(), app.state.state, ("album", "single"))
    assert len(with_singles[0]["missing"]) == 2

    page = client.get("/get/missing")
    balanced(page.text)
    balanced(client.get("/get/missing?show=all").text)
    assert "Missing One" in page.text and "欠けた一枚" in page.text
    assert "2 of 3 owned" in page.text

    client.post("/get/missing/dismiss", data={"source": "touhoudb", "release_id": "3"})
    [row] = discography.gaps(beets_ctx.lib(), app.state.state)
    assert not row["missing"] and [r["id"] for r in row["dismissed"]] == ["3"]
    assert "Missing One" not in client.get("/get/missing").text
    client.post("/get/missing/restore", data={"source": "touhoudb", "release_id": "3"})
    assert "Missing One" in client.get("/get/missing").text


@respx.mock
def test_musicbrainz_release_groups():
    respx.get("https://musicbrainz.org/ws/2/release-group").mock(
        return_value=httpx.Response(
            200,
            json={
                "release-group-count": 3,
                "release-groups": [
                    {"id": "b", "title": "Live!", "primary-type": "Album",
                     "secondary-types": ["Live"], "first-release-date": "2021"},
                    {"id": "a", "title": "Debut", "primary-type": "Album",
                     "secondary-types": [], "first-release-date": "2019-04-01"},
                    {"id": "c", "title": "Best Of", "primary-type": "Album",
                     "secondary-types": ["Compilation"], "first-release-date": "2022"},
                ],
            },
        )
    )  # fmt: skip
    out = discography.fetch_musicbrainz(discography.Client(), "x")
    assert [(r["id"], r["kind"]) for r in out] == [
        ("a", "album"),
        ("b", "other"),
        ("c", "compilation"),
    ]


# -------------------------------------------------------------- listening


def jellyfin_mock(paths_and_data):
    respx.get("http://jellyfin:8096/Users").mock(
        return_value=httpx.Response(
            200, json=[{"Name": "miguel", "Id": "u1", "Policy": {"IsAdministrator": True}}]
        )
    )
    items = [{"Name": os.path.basename(p), "Path": p, "UserData": ud} for p, ud in paths_and_data]
    return respx.get("http://jellyfin:8096/Items").mock(
        return_value=httpx.Response(200, json={"Items": items, "TotalRecordCount": len(items)})
    )


@respx.mock
def test_listening_sync_matches_paths_under_another_mount(app, client, env):
    played = add_album(env, album="Loved", n=2)
    old = add_album(env, album="Old Favourite", n=1)
    never = add_album(env, album="Untouched", n=1)
    music = os.fsdecode(beets_ctx.lib().directory)

    def jf(item):  # Jellyfin sees the music folder as /data/music
        return "/data/music" + os.fsdecode(item.path)[len(music) :]

    long_ago = time.strftime("%Y-%m-%dT%H:%M:%S.1234567Z", time.gmtime(time.time() - 800 * 86400))
    p1, p2 = sorted(played.items(), key=lambda i: i.track)
    route = jellyfin_mock(
        [
            (jf(p1), {"PlayCount": 5, "LastPlayedDate": "2026-11-01T10:00:00.0000000Z",
                      "IsFavorite": True}),
            (jf(p2), {"PlayCount": 2}),
            (jf(next(iter(old.items()))), {"PlayCount": 1, "LastPlayedDate": long_ago}),
            (jf(next(iter(never.items()))), {"PlayCount": 0}),
            ("/data/music/elsewhere/x.flac", {"PlayCount": 9}),
        ]
    )  # fmt: skip

    page = client.get("/insights/listening")  # first visit starts a sync
    assert "first time" in page.text
    app.state.jobs.run_pending_sync()
    assert 'MediaBrowser Client="beets-ui"' in route.calls[0].request.headers["authorization"]
    assert route.calls[0].request.url.params["userId"] == "u1"
    info = app.state.state.meta("jellyfin_sync")
    assert (info["matched"], info["unmatched"], info["user"]) == (4, 1, "miguel")

    data = listening.stats(beets_ctx.lib(), app.state.state)
    assert [a["album"] for a in data["never"]] == ["Untouched"]
    assert [a["album"] for a in data["stale"]] == ["Old Favourite"]
    assert data["top_albums"][0]["album"] == "Loved" and data["top_albums"][0]["plays"] == 7
    assert data["plays"] == 8 and data["favorites"] == 1

    page = client.get("/insights/listening")
    balanced(page.text)
    assert "3/3</span> albums played" not in page.text and "2/3</span> albums played" in page.text
    r = client.get("/insights/listening/random", follow_redirects=False)
    assert r.headers["location"] == f"/library/album/{never.id}"
    # Synced a moment ago: no new sync job.
    assert len(app.state.state.jobs(10, kind=listening.SYNC_KIND)) == 1


def test_jellyfin_dates():
    assert listening.parse_date("2024-05-01T12:00:00.1234567Z") == pytest.approx(1714564800.123456)
    assert listening.parse_date("") is None and listening.parse_date("nope") is None


# ------------------------------------------------------------------- jobs


def test_net_jobs_have_their_own_lane_and_log(app):
    import logging

    jobs = app.state.jobs
    seen = []

    @jobs.register("net-test", lane="net")
    def net_test(ctx):
        seen.append(jobs.running())
        logging.getLogger("beets").info("from the net job")

    job = jobs.submit("net-test", "net")
    assert jobs.pending() == 1
    jobs.run_pending_sync()
    assert seen == [[job]] and jobs.running() == []
    assert "from the net job" in app.state.state.job(job)["log"]
    with pytest.raises(ValueError):
        jobs.register("x", lane="nope")


def test_insights_pages_render(client):
    for url in ("/insights/upgrades", "/insights/listening", "/insights/lyrics"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert 'href="/insights" aria-current="page"' in r.text
    r = client.get("/get/missing")
    assert r.status_code == 200
    assert 'href="/get" aria-current="page"' in r.text


def test_get_section_and_old_addresses(client):
    assert client.get("/get", follow_redirects=False).headers["location"] == "/get/missing"
    assert (
        client.get("/insights", follow_redirects=False).headers["location"] == "/insights/upgrades"
    )
    r = client.get("/insights/gaps?show=all", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/get/missing?show=all"
    r = client.get("/inbox/find?q=a%20b", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/get/find?q=a%20b"
    # Get sits second in the sidebar; the inbox is for review.
    nav = client.get("/inbox").text
    assert nav.index('href="/get"') < nav.index('href="/inbox"')
    assert 'id="requests"' not in nav


def test_align_skips_lines_the_human_romaji_lacks():
    generated = ["Asa me ga samete", "Ureshiku nai tameiki wo tsuita", "Meruto tokete shimaisou"]
    human = ["asa me ga samete", "meruto tokete shimaisou", "suki da nante"]
    assert lyrics.align(generated, human) == [
        "asa me ga samete",
        None,
        "meruto tokete shimaisou",
    ]


def test_gaps_busy_poll_and_sort_links(app, client):
    # Nothing running: the poll tells htmx to reload the page.
    r = client.get("/get/missing/busy")
    assert r.headers.get("HX-Refresh") == "true"
    app.state.state.create_job("discography", "Look up discographies", {})
    assert client.get("/get/missing/busy").status_code == 204
    page = client.get("/get/missing?sort=name").text
    balanced(page)
    assert 'hx-get="/get/missing/busy"' in page

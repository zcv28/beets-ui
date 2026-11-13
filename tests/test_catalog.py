"""Find looks releases up in the catalogues (MusicBrainz, VocaDB): artists open into
every release of theirs to get all or some, releases into their tracklist. Coming in
narrows to one artist, Discover links an artist to their page, and the library's
artist page shows covers for what it is missing."""

# The Get tests' fixtures are imported below, which pytest needs and ruff reads as redefinitions.
# ruff: noqa: F811

from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from beets_ui import beets_ctx, discover, importer
from beets_ui.catalog import Catalog, _mb_query

from conftest import make_album

# The fakes and fixtures of the Get tests (pytest finds fixtures imported here).
from test_get import app, client, engine, want  # noqa: F401

FRAGMENT = {"HX-Request": "true"}
ARTIST = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
RG_ALBUM = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RG_EP = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
RG_SINGLE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
RG_OWNED = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


def credit(name=ARTIST, artist="Test Artist"):
    return [{"name": artist, "joinphrase": "", "artist": {"id": name, "name": artist}}]


def group(rid, title, primary="Album", date="2020-01-01", secondary=()):
    return {
        "id": rid,
        "title": title,
        "primary-type": primary,
        "secondary-types": list(secondary),
        "first-release-date": date,
        "artist-credit": credit(),
    }


GROUPS = [
    group(RG_ALBUM, "Big Album", date="2021-05-01"),
    group(RG_EP, "Small EP", primary="EP", date="2019-02-02"),
    group(RG_SINGLE, "A Single", primary="Single"),
    group(RG_OWNED, "Test Album", date="2018-01-01"),
]


def musicbrainz(request: httpx.Request) -> httpx.Response:
    """MusicBrainz as far as these tests ask it."""
    path = request.url.path
    q = dict(request.url.params)
    if "broken" in q.get("query", ""):
        return httpx.Response(500)
    if path == "/ws/2/artist" and "query" in q:
        return httpx.Response(
            200,
            json={
                "artists": [
                    {"id": ARTIST, "name": "Test Artist", "type": "Group", "country": "JP"},
                    {"id": OTHER, "name": "Someone Else", "disambiguation": "the other one"},
                ]
            },
        )
    if path == "/ws/2/release-group" and "query" in q:
        return httpx.Response(200, json={"release-groups": [GROUPS[0]]})
    if path == "/ws/2/release-group" and q.get("artist") == ARTIST:
        return httpx.Response(200, json={"release-groups": GROUPS, "release-group-count": 4})
    if path == "/ws/2/recording":
        rel = {
            "id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "title": "Big Album",
            "status": "Official",
            "date": "2021-05-01",
            "artist-credit": credit(),
            "release-group": {"id": RG_ALBUM, "primary-type": "Album", "title": "Big Album"},
        }
        comp = {
            **rel,
            "id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "title": "Best Of",
            "release-group": {
                "id": OTHER,
                "primary-type": "Album",
                "secondary-types": ["Compilation"],
            },
        }
        return httpx.Response(
            200,
            json={
                "recordings": [
                    {
                        "id": "12121212-1212-4121-8121-121212121212",
                        "title": "Opening Song",
                        "length": 245000,
                        "artist-credit": credit(),
                        "releases": [comp, rel],
                    }
                ]
            },
        )
    if path.startswith("/ws/2/release-group/"):
        rid = path.rsplit("/", 1)[-1]
        g = next((g for g in GROUPS if g["id"] == rid), None)
        return httpx.Response(200, json=g) if g else httpx.Response(404, json={})
    if path == "/ws/2/release" and q.get("release-group"):
        tracks = [{"title": f"Song {i}", "length": 180000 + i * 1000} for i in (1, 2)]
        return httpx.Response(
            200,
            json={
                "releases": [
                    {
                        "id": "r1",
                        "status": "Official",
                        "date": "2021",
                        "media": [{"tracks": tracks}],
                    }
                ]
            },
        )
    if path == f"/ws/2/artist/{ARTIST}":
        return httpx.Response(200, json={"id": ARTIST, "name": "Test Artist", "type": "Group"})
    return httpx.Response(404, json={})


def vocadb(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/artists":
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": 77,
                        "name": "Test Artist",
                        "defaultName": "テスト",
                        "artistType": "Circle",
                    }
                ]
            },
        )
    return httpx.Response(404, json={})


@pytest.fixture
def web(client, app, monkeypatch):
    """The client, with MusicBrainz and VocaDB answering and no waiting between requests."""
    monkeypatch.setattr("beets_ui.discography.time.sleep", lambda s: None)
    app.state.catalog = Catalog(app.state.state)
    app.state.source.searches = lambda: []
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="musicbrainz.org").mock(side_effect=musicbrainz)
        mock.route(host="vocadb.net").mock(side_effect=vocadb)
        mock.route(host="coverartarchive.org").mock(return_value=httpx.Response(404))
        yield client


def own_test_album(app):
    """Test Artist – Test Album in the library."""
    folder = make_album(app.state.settings.inbox / "seed")
    key = importer.preview_folder(str(folder))[0]["key"]
    importer.apply_folder(str(folder), {key: {"action": "apply", "id": "fake-1", "source": "Fake"}})
    return beets_ctx.lib().albums()[0]


# ------------------------------------------------------------------- Find


def test_find_looks_up_releases_and_keeps_files_by_hand(web):
    page = web.get("/get/find").text
    assert 'aria-current="page">Artists and releases' in page
    assert 'action="/get/find/search"' not in page
    page = web.get("/get/find?q=test+artist&src=vocadb").text
    for kind in ("artists", "releases", "tracks"):
        assert f"/get/find/lookup?kind={kind}&amp;q=test%20artist&amp;src=vocadb" in page
    # The catalogue chosen is kept for the next search.
    assert '<option value="vocadb" selected>' in web.get("/get/find").text
    # Files by hand: the download source's search, and what "Find by hand" links open.
    page = web.get("/get/find?by=hand").text
    assert 'aria-current="page">Files by hand' in page and 'action="/get/find/search"' in page
    page = web.get("/get/find?q=x&artist=A&album=B").text
    assert 'action="/get/find/search"' in page and "Finding" in page


def test_search_text_is_escaped_for_musicbrainz():
    assert _mb_query("AC/DC: live!") == "AC\\/DC\\: live\\!"


def test_find_releases_and_get_one(web, app):
    part = web.get("/get/find/lookup?kind=releases&q=big+album&src=musicbrainz").text
    assert f'href="/get/release/musicbrainz/{RG_ALBUM}"' in part
    assert f'href="/get/artist/musicbrainz/{ARTIST}?name=Test%20Artist"' in part
    assert 'action="/wanted/catalog"' in part
    r = web.post(
        "/wanted/catalog",
        data={"source": "musicbrainz", "release_id": RG_ALBUM},
        headers=FRAGMENT,
    )
    assert "Waiting to search" in r.text
    assert json.loads(r.headers["HX-Trigger"])["toast"]["title"] == "Getting Big Album"
    (w,) = app.state.state.wanted_list()
    assert (w["source"], w["release_id"], w["artist"], w["album"], w["year"]) == (
        "musicbrainz",
        RG_ALBUM,
        "Test Artist",
        "Big Album",
        "2021",
    )
    # Found again, it shows where its request stands instead of Get.
    part = web.get("/get/find/lookup?kind=releases&q=big+album&src=musicbrainz").text
    assert f'href="/wanted/{w["id"]}"' in part and 'action="/wanted/catalog"' not in part


def test_find_tracks_leads_to_their_release(web):
    part = web.get("/get/find/lookup?kind=tracks&q=opening+song&src=musicbrainz").text
    assert "Opening Song" in part and "4:05" in part
    # The album it is on, not the compilation.
    assert f'href="/get/release/musicbrainz/{RG_ALBUM}"' in part and "Best Of" not in part
    assert (
        "Find the track by hand" in part and "by=hand&amp;q=Test%20Artist%20Opening%20Song" in part
    )


def test_find_artists_says_which_are_in_the_library(web, app):
    own_test_album(app)
    part = web.get("/get/find/lookup?kind=artists&q=test&src=musicbrainz").text
    assert f'href="/get/artist/musicbrainz/{ARTIST}?name=Test%20Artist"' in part
    assert 'href="/library/artist?name=Test%20Artist">In your library' in part
    assert "the other one" in part
    part = web.get("/get/find/lookup?kind=artists&q=test&src=vocadb").text
    assert 'href="/get/artist/vocadb/77?name=Test%20Artist"' in part and "テスト" in part


def test_a_catalogue_that_fails_says_so(web):
    part = web.get("/get/find/lookup?kind=releases&q=broken&src=musicbrainz").text
    assert "search MusicBrainz" in part and "musicbrainz.org answered 500" in part


# ----------------------------------------------------------------- artist


def test_artist_page_gets_some_or_all_of_their_releases(web, app):
    own_test_album(app)
    page = web.get(f"/get/artist/musicbrainz/{ARTIST}?name=Test+Artist").text
    assert "<h1" in page and "Test Artist" in page
    assert f"/get/artist/musicbrainz/{ARTIST}/releases?name=Test%20Artist" in page
    assert "Their albums in your library" in page

    part = web.get(
        f"/get/artist/musicbrainz/{ARTIST}/releases?name=Test+Artist&kinds=album&kinds=ep"
    ).text
    # Albums and EPs, newest first; the one in the library can't be picked.
    assert part.index("Big Album") < part.index("Small EP") < part.index("Test Album")
    assert "A Single" not in part
    assert f'value="{RG_ALBUM}" form="pick-releases"' in part
    assert f'value="{RG_OWNED}" form="pick-releases"' not in part
    assert "In your library" in part and "Get all 2 you don't have" in part
    # Looked up once: the next visit needs no lookup.
    assert app.state.state.discography("musicbrainz", ARTIST)["artist"] == "Test Artist"

    back = f"/get/artist/musicbrainz/{ARTIST}?name=Test%20Artist"
    data = {"source": "musicbrainz", "artist_id": ARTIST, "name": "Test Artist", "back": back}
    r = web.post("/wanted/catalog-artist", data={**data, "id": [RG_EP]}, follow_redirects=False)
    assert r.headers["location"] == back
    assert [w["album"] for w in app.state.state.wanted_list()] == ["Small EP"]
    # Nothing ticked: nothing asked for.
    web.post("/wanted/catalog-artist", data=data)
    assert len(app.state.state.wanted_list()) == 1
    # All of them: only what isn't owned or asked for yet, of the kinds shown.
    web.post("/wanted/catalog-artist", data={**data, "all": "1", "kinds": ["album", "ep"]})
    got = sorted(w["album"] for w in app.state.state.wanted_list())
    assert got == ["Big Album", "Small EP"]
    part = web.get(f"/get/artist/musicbrainz/{ARTIST}/releases?name=Test+Artist").text
    assert "Get all" not in part and "Waiting to search" in part


PNG = b"\x89PNG\r\n\x1a\n" + bytes(32)


class _Artwork:
    """Artwork's MBID sources, as far as artist pictures need them."""

    def __init__(self, found):
        self.found, self.asked = found, []

    def enabled(self, kind, override=None):
        from beets_ui.artwork.sources import ImageCandidate

        outer = self

        class Src:
            def artist_images(self, q):
                outer.asked.append((kind, tuple(override or ()), q.mb_artistid, q.name))
                if isinstance(outer.found, Exception):
                    raise outer.found
                return [
                    ImageCandidate(url=u, thumb=u + "?thumb", source="x", match=m)
                    for u, m in outer.found
                ]

        return [Src()]


@respx.mock
def test_artist_pictures_come_by_id_and_are_kept(tmp_path):
    from beets_ui.artist_pictures import ArtistPictures

    respx.get("https://vocadb.net/api/artists/77").mock(
        return_value=httpx.Response(200, json={"mainPicture": {"urlThumb": "https://s/77.png"}})
    )
    respx.get("https://vocadb.net/api/artists/78").mock(return_value=httpx.Response(200, json={}))
    img = respx.get("https://s/77.png").mock(return_value=httpx.Response(200, content=PNG))
    pics = ArtistPictures(tmp_path, _Artwork([]), httpx.Client())
    path, kind = pics.get("vocadb", "77")
    assert kind == "image/png" and path.read_bytes() == PNG
    pics.get("vocadb", "77")
    assert img.call_count == 1
    assert pics.get("vocadb", "78") is None  # no picture: remembered
    assert pics.get("vocadb", "78") is None
    assert respx.calls.call_count == 3
    assert pics.get("vocadb", "../x") is None and pics.get("musicbrainz", "nope") is None

    # MusicBrainz: only a picture found through the artist's ID, never a name search.
    aw = _Artwork([("https://n/by-name.png", "name"), ("https://i/by-id.png", "id")])
    respx.get("https://i/by-id.png?thumb").mock(return_value=httpx.Response(200, content=PNG))
    pics = ArtistPictures(tmp_path / "mb", aw, httpx.Client())
    assert pics.get("musicbrainz", ARTIST)[1] == "image/png"
    assert aw.asked == [("artist", ("fanarttv", "wikidata"), ARTIST, "")]
    only_name = ArtistPictures(tmp_path / "n", _Artwork([("https://n/x.png", "name")]))
    assert only_name.get("musicbrainz", OTHER) is None
    # A source that couldn't be asked is asked again next time.
    down = _Artwork(RuntimeError("timeout"))
    pics = ArtistPictures(tmp_path / "d", down, httpx.Client())
    assert pics.get("musicbrainz", ARTIST) is None and pics.get("musicbrainz", ARTIST) is None
    assert len(down.asked) == 2


def test_artist_page_shows_a_picture_without_their_albums(web, app, tmp_path):
    from beets_ui.artist_pictures import ArtistPictures

    page = web.get(f"/get/artist/musicbrainz/{ARTIST}?name=Test+Artist").text
    assert f'src="/get/artist/musicbrainz/{ARTIST}/picture"' in page
    app.state.artist_pictures = ArtistPictures(tmp_path / "p", _Artwork([]))
    r = web.get(f"/get/artist/musicbrainz/{ARTIST}/picture")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg")
    assert web.get("/get/artist/musicbrainz/nope/picture").status_code == 404


def test_artist_of_a_release_opens_their_page(web):
    r = web.get(f"/get/artist/of/musicbrainz/{RG_ALBUM}", follow_redirects=False)
    assert r.headers["location"] == f"/get/artist/musicbrainz/{ARTIST}?name=Test%20Artist"
    assert web.get("/get/artist/musicbrainz/not-an-id").status_code == 404


# ---------------------------------------------------------------- release


def test_release_page_shows_its_tracklist(web, app):
    page = web.get(f"/get/release/musicbrainz/{RG_ALBUM}").text
    assert "Big Album" in page and f"/get/release/musicbrainz/{RG_ALBUM}/tracks" in page
    assert f"/get/artist/musicbrainz/{ARTIST}?name=Test%20Artist" in page
    assert 'action="/wanted/catalog"' in page and "Find by hand" in page
    tracks = web.get(f"/get/release/musicbrainz/{RG_ALBUM}/tracks").text
    assert "Song 1" in tracks and "Song 2" in tracks and "3:01" in tracks
    assert web.get(f"/get/release/musicbrainz/{OTHER}").status_code == 404


# -------------------------------------------------------------- Coming in


def test_coming_in_narrows_to_one_artist(web, app):
    state = app.state.state
    want(state, artist="Alpha", album="First", release_id="1")
    want(state, artist="Alpha", album="Second", release_id="2")
    want(state, artist="Beta", album="Third", release_id="3")
    page = web.get("/get/coming").text
    assert ">Alpha · 2</option>" in page and ">Beta · 1</option>" in page
    assert 'href="/get/coming?artist=Alpha">All 2 by' in page
    assert 'href="/get/artist/of/vocadb/1"' in page
    page = web.get("/get/coming?artist=Alpha").text
    assert "First" in page and "Second" in page and "Third" not in page
    assert 'hx-get="/get/coming/list?artist=Alpha"' in page
    # Refreshed or re-sent in place, the list keeps to the artist of its page.
    part = web.get(
        "/get/coming/list",
        headers={**FRAGMENT, "HX-Current-URL": "http://testserver/get/coming?artist=Alpha"},
    ).text
    assert "Third" not in part and "Second" in part
    assert "Nothing by Gamma" in web.get("/get/coming?artist=Gamma").text


# ---------------------------------------------------------------- Discover


def test_discover_links_the_artist(web, app):
    album = {
        "id": RG_ALBUM,
        "title": "Big Album",
        "artist": "Test Artist",
        "year": "2021",
        "tracks": [],
        "lists": ["Weekly Exploration"],
    }
    app.state.state.set_meta(
        discover.META_KEY, {"at": time.time(), "user": "me", "playlists": [], "albums": [album]}
    )
    app.state.settings.listenbrainz_user = "me"
    page = web.get("/get/discover").text
    assert f'href="/get/artist/of/musicbrainz/{RG_ALBUM}"' in page
    assert f'href="/get/release/musicbrainz/{RG_ALBUM}"' in page


# ----------------------------------------------------------- library artist


def test_library_artist_page_shows_covers_of_what_is_missing(web, app):
    a = own_test_album(app)
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
    app.state.state.set_discography("vocadb", "77", "Test Artist", [rel])
    page = web.get("/library/artist?name=Test+Artist").text
    assert 'src="/get/cover/vocadb/500"' in page
    assert 'href="/get/release/vocadb/500"' in page
    assert 'href="/get/artist/vocadb/77?name=Test%20Artist"' in page


def test_japanese_artists_show_romanized():
    from beets_ui.catalog import _mb_artist

    a = _mb_artist({"id": ARTIST, "name": "ヨルシカ", "sort-name": "Yorushika", "type": "Group"})
    assert (a["name"], a["original"], a["detail"]) == ("Yorushika", "ヨルシカ", "Group")
    # A Latin name stays as it is, whatever its sort name ("Beatles, The").
    a = _mb_artist({"id": ARTIST, "name": "The Beatles", "sort-name": "Beatles, The"})
    assert (a["name"], a["original"]) == ("The Beatles", "")


# ---------------------------------------------------------------- hardening


def test_ownership_is_yours_maybe_or_not(web, app):
    from beets_ui import discography

    a = own_test_album(app)  # Test Artist – Test Album, 2020
    index = discography.owned_index(beets_ctx.lib())
    own = discography.ownership
    assert own(index, ["x"], ["Test Artist"], ["Test Album"]) == "yours"
    # Another edition, or credited to someone else: maybe.
    assert own(index, ["x"], ["Someone"], ["Test Album (Deluxe Edition)"]) == "maybe"
    # Theirs from the same year under its name as written: maybe.
    assert own(index, ["x"], ["Test Artist"], ["テストアルバム"], "2020-05-01") == "maybe"
    # Another album of theirs that year, in the same script: not.
    assert own(index, ["x"], ["Test Artist"], ["Other Album"], "2020-05-01") == ""
    # A maybe names the album it suspects.
    state, suspect = discography.match(index, ["x"], ["Test Artist"], ["テストアルバム"], "2020")
    assert (state, suspect) == ("maybe", {"id": a.id, "album": "Test Album", "year": 2020})


def test_artist_page_leaves_maybe_out_of_get_all(web, app):
    a = own_test_album(app)
    a["year"] = 2021
    a.store()
    part = web.get(f"/get/artist/musicbrainz/{ARTIST}/releases?name=Test+Artist").text
    # Big Album is from 2021 like the library's Test Album, but both are romanized: not
    # a maybe. Test Album itself is yours by title.
    assert "In your library" in part and "Maybe your" not in part
    # Rename the library's album to its "romanized" self and the catalogue's to Japanese.
    a["album"] = "Biggu Arubamu"
    a.store()
    GROUPS[0]["title"] = "ビッグアルバム"
    try:
        web.get(f"/get/artist/musicbrainz/{ARTIST}/releases?name=Test+Artist&fresh=1")
        part = web.get(f"/get/artist/musicbrainz/{ARTIST}/releases?name=Test+Artist").text
    finally:
        GROUPS[0]["title"] = "Big Album"
    assert "Get all 2 you don't have" in part
    # The row names the album it may be, linked; that album isn't listed as unmatched.
    assert f'Maybe your <a href="/library/album/{a.id}">Biggu Arubamu</a>' in part
    assert "matched nothing here" not in part
    data = {"source": "musicbrainz", "artist_id": ARTIST, "name": "Test Artist"}
    web.post("/wanted/catalog-artist", data={**data, "all": "1"})
    assert sorted(w["album"] for w in app.state.state.wanted_list()) == ["Small EP", "Test Album"]
    # Ticked by hand, a maybe is got.
    web.post("/wanted/catalog-artist", data={**data, "id": [RG_ALBUM]})
    assert len(app.state.state.wanted_list()) == 3


def test_getting_many_can_be_undone(web, app):
    data = {"source": "musicbrainz", "artist_id": ARTIST, "name": "Test Artist", "all": "1"}
    data["kinds"] = ["album", "ep", "single"]
    page = web.post("/wanted/catalog-artist", data=data).text
    state = app.state.state
    ids = [w["id"] for w in state.wanted_list()]
    assert len(ids) == 4 and 'action="/wanted/undo"' in page and ">Undo</button>" in page
    assert f'value="{",".join(map(str, ids))}"' in page
    state.update_wanted(ids[0], status="searching")
    page = web.post("/wanted/undo", data={"ids": ",".join(map(str, ids))}).text
    assert [w["id"] for w in state.wanted_list()] == [ids[0]]
    assert "Took back 3 requests" in page and "1 had started already" in page


def test_the_same_album_is_asked_for_once(app):
    from beets_ui import acquire

    state = app.state.state
    row = {"source": "vocadb", "artist": "Test Artist", "artist_original": ""}
    first, made = acquire.from_gap(state, row, {"id": "500", "title": "Big Album", "date": ""})
    assert made
    row = {"source": "musicbrainz", "artist": "test artist", "artist_original": ""}
    again, made = acquire.from_gap(state, row, {"id": RG_ALBUM, "title": "BIG ALBUM", "date": ""})
    assert (again, made) == (first, False)


def test_back_link_follows_where_you_came_from(web):
    url = f"/get/artist/musicbrainz/{ARTIST}?name=Test+Artist"
    page = web.get(url, headers={"Referer": "http://testserver/get/coming?artist=A"}).text
    assert 'href="/get/coming?artist=A"' in page and "Coming in" in page
    # Back on the page after an action on it: still where you came from.
    page = web.get(url, headers={"Referer": f"http://testserver{url}"}).text
    assert 'href="/get/coming?artist=A"' in page
    page = web.get(url, headers={"Referer": "http://testserver/get/find?q=yoru"}).text
    assert "Find: yoru" in page


def test_better_copies_in_one_go(web, app):
    a = own_test_album(app)
    r = web.post("/wanted/albums", data={"album_id": [a.id]}, follow_redirects=False)
    assert r.headers["location"] == "/insights/upgrades"
    (w,) = app.state.state.wanted_list()
    assert (w["kind"], w["album_id"]) == ("upgrade", a.id)


# ------------------------------------------------------------ open items


def test_a_pick_whose_copies_are_gone_says_so_everywhere(web, app):
    state = app.state.state
    wid = want(
        state, album="Gone", status="choosing", status_text="Found 4 copies, none clearly right"
    )
    coming = web.get("/get/coming").text
    assert "Its copies are gone" in coming and "The copies it found are no longer listed" in coming
    # The request page says why it stopped (the label shows where its state is a chip).
    request = web.get(f"/wanted/{wid}").text
    assert "The copies it found are no longer listed" in request
    for page in (coming, request):
        assert "Found 4 copies" not in page and "Waiting for your pick" not in page
    home = web.get("/home/now").text
    assert "Search again for Gone" in home and "no longer listed" in home


def test_remove_can_be_undone(web, app):
    state = app.state.state
    wid = want(state, album="Old", status="failed", status_text="Nothing found")
    state.update_wanted(wid, history=[{"at": 1.0, "text": "Searched"}])
    r = web.post(f"/wanted/{wid}/remove", headers=FRAGMENT)
    undo = json.loads(r.headers["HX-Trigger"])["toast"]["undo"]
    assert undo["url"] == f"/wanted/{wid}/restore" and state.wanted(wid) is None
    web.post(undo["url"], headers=FRAGMENT)
    w = state.wanted(wid)
    assert (w["album"], w["status"], w["history"][0]["text"]) == ("Old", "failed", "Searched")
    # Only once: it is back already.
    assert "can&#39;t be brought back" in web.post(f"/wanted/{wid}/restore").text
    # Cancel and Remove read as destructive.
    page = web.get("/get/coming").text
    assert 'data-tone="destructive" data-size="sm">Remove from the list</button>' in page


def test_every_get_tab_says_where_getting_stands(web, app):
    state = app.state.state
    want(state, album="Stuck", status="failed", status_text="Nothing found", release_id="s1")
    want(state, album="Moving", status="searching", release_id="s2")
    want(state, album="Waiting", release_id="s3")
    for url in ("/get/missing", "/get/discover", "/get/coming", "/get/find"):
        page = web.get(url).text
        assert '<a href="/get/coming">1 needs you</a> · 2 on their way' in page, url

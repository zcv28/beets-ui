"""Romanized main names with the originals kept alongside."""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from mediafile import MediaFile

from beets_ui import beets_ctx, importer, romanize
from beets_ui.settings import Settings

from conftest import make_album

JP = {
    "album": "東方紅魔郷",
    "artist": "上海アリス幻樂団",
    "titles": ["赤より紅い夢", "ほおずきみたいに紅い魂"],
}
RO = {
    "album": "Touhou Koumakyou",
    "artist": "Shanghai Alice Gensoukyoku",
    "titles": ["Aka yori Akai Yume", "Hoozuki Mitai ni Akai Tamashii"],
}


def album_info(names: dict, album_id="42", source="VocaDB"):
    from beets.autotag import AlbumInfo, TrackInfo

    tracks = [
        TrackInfo(
            title=t,
            track_id=f"s{i}",
            artist=names["artist"],
            index=i,
            medium=1,
            medium_index=i,
            length=1.0,
            data_source=source,
        )
        for i, t in enumerate(names["titles"], 1)
    ]
    return AlbumInfo(
        tracks=tracks,
        album=names["album"],
        album_id=album_id,
        artist=names["artist"],
        year=2002,
        data_source=source,
        mediums=1,
    )


@pytest.fixture
def pair(monkeypatch):
    calls = []

    def fake(source, album_id):
        calls.append((source, album_id))
        if album_id == "none":
            return None
        return album_info(RO, album_id), album_info(JP, album_id)

    monkeypatch.setattr(romanize, "fetch_pair", fake)
    return calls


@pytest.fixture
def app(env, fake_source, monkeypatch):
    from beets_ui.main import create_app

    monkeypatch.setenv("BEETSUI_ROMANIZE", "true")
    return create_app(Settings.from_env(), background=False)


@pytest.fixture
def client(app):
    c = TestClient(app)
    c.post("/login", data={"password": "secret"})
    return c


def run_jobs(app):
    app.state.jobs.run_pending_sync()


def last_log(app):
    state = app.state.state
    return state.job(state.jobs()[0]["id"])["log"]


def test_romanize_info_keeps_originals(pair):
    info = album_info(JP)
    assert romanize.romanize_info(info)
    assert (info.album, info.artist, info.artists) == (RO["album"], RO["artist"], [RO["artist"]])
    assert info["album_original"] == JP["album"]
    assert info["albumartist_original"] == JP["artist"]
    assert [t.title for t in info.tracks] == RO["titles"]
    assert [t["title_original"] for t in info.tracks] == JP["titles"]
    assert info.tracks[0]["artist_original"] == JP["artist"]
    # Nothing romanized at the source: nothing changes.
    plain = album_info(JP, album_id="none")
    assert not romanize.romanize_info(plain)
    assert plain.album == JP["album"] and "album_original" not in plain


def test_import_writes_romanized_names_and_original_tags(app, client, env, fake_source, pair):
    fake_source["42"] = album_info(JP)
    folder = make_album(env["inbox"] / "eosd", artist=JP["artist"], album=JP["album"], n=2)
    client.post("/inbox/scan")
    client.post("/inbox/folder/preview", data={"f": "eosd", "ids": "42"})
    run_jobs(app)
    row = app.state.state.folder("eosd")
    idx = next(i for i, c in enumerate(row["tasks"][0]["candidates"]) if c["id"] == "42")
    client.post("/inbox/folder/import", data={"f": "eosd", "choice-0": f"c:{idx}"})
    run_jobs(app)
    assert app.state.state.folder("eosd")["status"] == "imported"
    assert ", romanized names" in last_log(app)

    album = beets_ctx.lib().albums()[0]
    assert (album.album, album.albumartist) == (RO["album"], RO["artist"])
    assert album["album_original"] == JP["album"]
    assert album["albumartist_original"] == JP["artist"]
    items = sorted(album.items(), key=lambda i: i.track)
    assert [i.title for i in items] == RO["titles"]
    assert [i.title_original for i in items] == JP["titles"]
    mf = MediaFile(str(items[0].filepath))
    assert (mf.title, mf.artist, mf.album) == (RO["titles"][0], RO["artist"], RO["album"])
    assert (mf.title_original, mf.album_original) == (JP["titles"][0], JP["album"])
    assert items[0].filepath.parent == env["music"] / RO["artist"] / RO["album"]
    assert folder.exists()

    # Both versions are shown and searchable.
    page = client.get(f"/library/album/{album.id}").text
    assert JP["album"] in page and RO["titles"][0] in page and JP["titles"][1] in page
    assert RO["album"] in client.get("/library?q=" + JP["album"]).text
    assert RO["titles"][1] in client.get("/library/tracks?q=" + JP["titles"][1]).text
    assert RO["album"] in client.get("/library?q=Koumakyou").text
    assert JP["artist"] in client.get("/library/artists").text


def test_import_without_setting_keeps_source_names(env, fake_source, pair):
    fake_source["42"] = album_info(JP)
    folder = make_album(env["inbox"] / "eosd", artist=JP["artist"], album=JP["album"], n=2)
    key = importer.preview_folder(str(folder), search_ids=["42"])[0]["key"]
    importer.apply_folder(str(folder), {key: {"action": "apply", "id": "42", "source": "VocaDB"}})
    assert beets_ctx.lib().albums()[0].album == JP["album"]
    assert pair == []


def test_romanize_library_job_and_undo(app, client, env, pair):
    lib = beets_ctx.lib()
    folder = make_album(env["inbox"] / "x", artist=JP["artist"], album=JP["album"], n=2)
    key = importer.preview_folder(str(folder))[0]["key"]
    importer.apply_folder(str(folder), {key: {"action": "asis"}})
    album = lib.albums()[0]
    album.data_source, album.mb_albumid = "VocaDB", "42"
    album.store()
    for n, item in enumerate(sorted(album.items(), key=lambda i: i.track), 1):
        item.title, item.mb_trackid = JP["titles"][n - 1], f"s{n}"
        item.store()
    old_artist_dir = env["music"] / JP["artist"]
    (old_artist_dir / "folder.jpg").write_bytes(b"jpg")

    page = client.get("/library/romanize").text
    assert "0 of 1 supported album" in page
    r = client.post(
        "/library/romanize",
        data={"write": "1", "move": "1", "only_new": "1"},
        follow_redirects=False,
    )
    job_id = int(r.headers["location"].rsplit("/", 1)[1])
    run_jobs(app)
    job = app.state.state.job(job_id)
    assert job["status"] == "done", job["log"]
    assert "Romanized 1 album(s)" in job["log"]

    album = lib.get_album(album.id)
    assert (album.album, album.albumartist) == (RO["album"], RO["artist"])
    assert album.album_original == JP["album"]
    items = sorted(album.items(), key=lambda i: i.track)
    assert [i.title for i in items] == RO["titles"]
    assert [i.title_original for i in items] == JP["titles"]
    assert items[0].filepath.parent == env["music"] / RO["artist"] / RO["album"]
    assert MediaFile(str(items[0].filepath)).title_original == JP["titles"][0]
    # The artist picture moved with the folder; the old folder is gone.
    assert (env["music"] / RO["artist"] / "folder.jpg").read_bytes() == b"jpg"
    assert not old_artist_dir.exists()
    assert "1 of 1 supported album" in client.get("/library/romanize").text

    # Running again changes nothing.
    client.post("/library/romanize", data={"write": "1", "move": "1"})
    run_jobs(app)
    assert "already romanized" in last_log(app)

    client.post(f"/jobs/{job_id}/undo")
    run_jobs(app)
    album = lib.get_album(album.id)
    assert (album.album, album.albumartist) == (JP["album"], JP["artist"])
    assert "album_original" not in album
    items = sorted(album.items(), key=lambda i: i.track)
    assert [i.title for i in items] == JP["titles"]
    assert "title_original" not in items[0]
    assert items[0].filepath.parent == env["music"] / JP["artist"] / JP["album"]


def test_romanize_skips_latin_albums_and_reports_missing_romaji(app, client, env, pair):
    lib = beets_ctx.lib()
    for name, source, mbid in (("Latin", "Discogs", "1"), ("ナンバーガール", "VocaDB", "none")):
        folder = make_album(env["inbox"] / name, artist=name, album=name)
        key = importer.preview_folder(str(folder))[0]["key"]
        importer.apply_folder(str(folder), {key: {"action": "asis"}})
        album = next(a for a in lib.albums() if a.album == name)
        album.data_source, album.mb_albumid = source, mbid
        album.store()
    client.post("/library/romanize", data={"only_new": "1"})
    run_jobs(app)
    log = last_log(app)
    assert "ナンバーガール - ナンバーガール: no romanized names found" in log
    assert "Romanized 0 album(s); 1 still have names nobody has romanized, 1 were in Latin" in log
    assert pair == [("VocaDB", "none")]  # the Latin album needed no lookup


def test_mbsync_button_romanizes_again(app, client, env):
    folder = make_album(env["inbox"] / "x")
    key = importer.preview_folder(str(folder))[0]["key"]
    importer.apply_folder(str(folder), {key: {"action": "asis"}})
    album = beets_ctx.lib().albums()[0]
    album.data_source = "MusicBrainz"
    album.store()
    client.post(f"/library/album/{album.id}/run", data={"action": "mbsync"})
    kinds = [j["kind"] for j in app.state.state.jobs()]
    assert kinds == ["romanize", "console"]
    assert 'action="/library/album/' in client.get(f"/library/album/{album.id}").text


def test_originals_query():
    q = romanize.originals_query
    assert q(["foo", "bar"], ("a", "b")) == [
        "foo",
        "bar",
        ",",
        "a:foo",
        "a:bar",
        ",",
        "b:foo",
        "b:bar",
    ]
    assert q(["artist:foo"], ("a",)) == ["artist:foo"]
    assert q(["x"], ("a",), always=["singleton:true"]) == [
        "x",
        "singleton:true",
        ",",
        "a:x",
        "singleton:true",
    ]
    assert q([], ("a",)) == []


# --------------------------------------------------------- source lookups


def test_musicbrainz_pair_from_official_and_pseudo_release():
    releases = {
        "official": {
            "id": "official",
            "status": "Official",
            "text_representation": {"script": "Jpan"},
            "release_relations": [
                {
                    "type": "transl-tracklisting",
                    "direction": "forward",
                    "release": {"id": "kana", "text_representation": {"script": "Kana"}},
                },
                {
                    "type": "transl-tracklisting",
                    "direction": "forward",
                    "release": {"id": "latin", "text_representation": {"script": "Latn"}},
                },
            ],
        },
        "latin": {
            "id": "latin",
            "status": "Pseudo-Release",
            "text_representation": {"script": "Latn"},
            "release_relations": [
                {
                    "type": "transl-tracklisting",
                    "direction": "backward",
                    "release": {"id": "official"},
                },
            ],
        },
        "english": {
            "id": "english",
            "status": "Official",
            "text_representation": {"script": "Latn"},
        },
        "lonely": {"id": "lonely", "status": "Official", "text_representation": {"script": "Jpan"}},
    }
    plugin = SimpleNamespace(
        mb_api=SimpleNamespace(get_release=lambda i: releases[i]),
        album_info=lambda raw: raw["id"],
    )
    assert romanize._musicbrainz(plugin, "official") == ("latin", "official")
    assert romanize._musicbrainz(plugin, "latin") == ("latin", "official")
    assert romanize._musicbrainz(plugin, "english") is None
    assert romanize._musicbrainz(plugin, "lonely") is None


@respx.mock
def test_vocadb_lookup_asks_for_both_languages(monkeypatch):
    seen = []

    def album_info(data, search_lang=None):
        seen.append(search_lang)
        return data["name"]

    plugin = SimpleNamespace(
        data_source="TouhouDB",
        instance_info=SimpleNamespace(api_url="https://touhoudb.com/api/"),
        song_fields="Artists",
        headers={"accept": "application/json"},
        album_info=album_info,
    )
    monkeypatch.setattr(romanize, "_plugin", lambda source: plugin)
    route = respx.get(url__startswith="https://touhoudb.com/api/albums/7").mock(
        side_effect=lambda req: httpx.Response(
            200, json={"name": "Romaji" if "lang=Romaji" in str(req.url) else "日本語"}
        )
    )
    assert romanize.fetch_pair("TouhouDB", "7") == ("Romaji", "日本語")
    assert seen == ["Romaji", "Japanese"] and route.call_count == 2
    assert romanize.fetch_pair("Discogs", "7") is None


def test_romanized_import_follows_alias_saved_under_original_name(env, fake_source, pair):
    fake_source["42"] = album_info(JP)
    folder = make_album(env["inbox"] / "eosd", artist=JP["artist"], album=JP["album"], n=2)
    key = importer.preview_folder(str(folder), search_ids=["42"])[0]["key"]
    importer.apply_folder(
        str(folder),
        {key: {"action": "apply", "id": "42", "source": "VocaDB"}},
        aliases={JP["artist"].casefold(): "ZUN"},
        romanize=True,
    )
    album = beets_ctx.lib().albums()[0]
    assert album.albumartist == "ZUN" and album.album == RO["album"]


# --------------------------------------------------------------- artists

YORUSHIKA = {
    "id": "11111111-1111-1111-1111-111111111111",
    "name": "ヨルシカ",
    "sort_name": "Yorushika",
    "type": "Group",
    "aliases": [
        {"name": "YORUSHIKA", "locale": None, "type": None, "primary": None},
        {"name": "Yorushika", "locale": "en", "type": "Artist name", "primary": True},
        {"name": "ヨルシカ", "locale": "ja", "type": "Artist name", "primary": True},
    ],
}
NUMBER_GIRL = {
    "id": "22222222-2222-2222-2222-222222222222",
    "name": "NUMBER GIRL",
    "sort_name": "NUMBER GIRL",
    "type": "Group",
    "aliases": [{"name": "ナンバーガール", "locale": "ja", "type": "Artist name", "primary": True}],
}


def test_is_latin_and_latin_name():
    assert romanize.is_latin("Ryūichi Sakamoto & co.") and not romanize.is_latin("ヨルシカ")
    assert not romanize.is_latin("파란노을")
    assert romanize.latin_name(YORUSHIKA) == "Yorushika"
    assert romanize.latin_name(NUMBER_GIRL) == "NUMBER GIRL"
    person = {"name": "米津玄師", "sort_name": "Yonezu, Kenshi", "type": "Person", "aliases": []}
    assert romanize.latin_name(person) == "Kenshi Yonezu"
    hint = {
        "name": "宇宙ネコ子",
        "sort_name": "宇宙ネコ子",
        "aliases": [{"name": "Uchuu Nekoko", "type": "Search hint", "primary": None}],
    }
    assert romanize.latin_name(hint) == ""


class FakeApi:
    api_root = "https://musicbrainz.org/ws/2"

    def __init__(self, artists):
        self.artists = {a["id"]: a for a in artists}
        self.calls = []

    def get_json(self, url, params=None):
        self.calls.append(url)
        return self.artists[url.rsplit("/", 1)[1]]

    def search(self, entity, filters, limit=None):
        self.calls.append(("search", filters[""]))
        return list(self.artists.values())


@pytest.fixture
def mb(monkeypatch):
    api = FakeApi([YORUSHIKA, NUMBER_GIRL])
    monkeypatch.setattr(romanize.ArtistRomanizer, "_api", staticmethod(lambda: api))
    return api


def test_artist_romanizer(app, mb):
    r = romanize.ArtistRomanizer(app.state.state)
    assert r.resolve("ヨルシカ", YORUSHIKA["id"]) == "Yorushika"
    assert r.resolve("ナンバーガール") == "NUMBER GIRL"  # by alias, one match
    assert r.resolve("ヨルシカ & ナンバーガール") == ""  # joined credit: not looked up
    assert r.resolve("ナンバーガール", YORUSHIKA["id"]) == ""  # the ID is another artist
    assert r.resolve("Already Latin") == ""
    assert r.resolve("サテライト・ラヴァーズ") == ""  # no match
    calls = len(mb.calls)
    # Cached in state.db: a new resolver needs no lookups.
    again = romanize.ArtistRomanizer(app.state.state)
    assert again.resolve("ナンバーガール") == "NUMBER GIRL"
    assert again.resolve("サテライト・ラヴァーズ") == ""
    assert len(mb.calls) == calls


def test_romanize_job_romanizes_artists_of_any_album(app, client, env, mb):
    lib = beets_ctx.lib()
    folder = make_album(env["inbox"] / "x", artist="ヨルシカ", album="Elma", n=2)
    key = importer.preview_folder(str(folder))[0]["key"]
    importer.apply_folder(str(folder), {key: {"action": "asis"}})
    album = lib.albums()[0]
    album.mb_albumartistid = YORUSHIKA["id"]
    album.store()
    client.post("/library/romanize", data={"only_new": "1", "write": "1", "move": "1"})
    run_jobs(app)
    assert "Romanized 1 album(s)" in last_log(app)
    album = lib.get_album(album.id)
    assert album.albumartist == "Yorushika" and album.albumartist_original == "ヨルシカ"
    items = list(album.items())
    assert {i.artist for i in items} == {"Yorushika"}
    assert {i.artist_original for i in items} == {"ヨルシカ"}
    assert items[0].filepath.parent == env["music"] / "Yorushika" / "Elma"
    assert MediaFile(str(items[0].filepath)).artist == "Yorushika"


def test_import_romanizes_artists(env, fake_source, mb):
    info = album_info({"album": "Elma", "artist": "ヨルシカ", "titles": ["Hitchcock", "Elma"]})
    info.data_source = "Fake"
    for t in info.tracks:
        t.data_source = "Fake"
    fake_source["elma"] = info
    info.album_id = "elma"
    folder = make_album(env["inbox"] / "elma", artist="ヨルシカ", album="Elma", n=2)
    key = importer.preview_folder(str(folder), search_ids=["elma"])[0]["key"]
    out = importer.apply_folder(
        str(folder), {key: {"action": "apply", "id": "elma", "source": "Fake"}}, romanize=True
    )
    assert ", romanized artists" in out[key].detail
    album = beets_ctx.lib().albums()[0]
    romanize.copy_originals_to_albums()
    album.load()
    assert album.albumartist == "Yorushika" and album.albumartist_original == "ヨルシカ"
    assert {i.artist for i in album.items()} == {"Yorushika"}


def test_rename_one_artist_by_hand(app, client, env):
    folder = make_album(env["inbox"] / "s", artist="サテライト・ラヴァーズ", album="S")
    key = importer.preview_folder(str(folder))[0]["key"]
    importer.apply_folder(str(folder), {key: {"action": "asis"}})
    r = client.post(
        "/library/artists/merge",
        data={
            "name": ["サテライト・ラヴァーズ"],
            "target": "Satellite Lovers",
            "write": "1",
            "tracks": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/jobs/" in r.headers["location"]
    run_jobs(app)
    album = beets_ctx.lib().albums()[0]
    assert album.albumartist == "Satellite Lovers"
    assert album.albumartist_original == "サテライト・ラヴァーズ"
    assert {i.artist_original for i in album.items()} == {"サテライト・ラヴァーズ"}
    assert app.state.state.artist_aliases() == {"サテライト・ラヴァーズ": "Satellite Lovers"}


KESSOKU = {
    "id": "33333333-3333-3333-3333-333333333333",
    "name": "結束バンド",
    "sort_name": "Kessoku Band",
    "type": "Group",
    "aliases": [{"name": "kessoku band", "locale": "en", "type": "Artist name", "primary": True}],
}


def _two_kessoku_albums(env):
    for album in ("Kessoku Band", "Seisyun Complex"):
        folder = make_album(env["inbox"] / album, artist="結束バンド", album=album, n=2)
        key = importer.preview_folder(str(folder))[0]["key"]
        importer.apply_folder(str(folder), {key: {"action": "asis"}})
    (env["music"] / "結束バンド" / "folder.jpg").write_bytes(b"\xff\xd8picture")


def test_picture_survives_beets_cleaning_the_old_folder(app, client, env, monkeypatch):
    from beets import config

    api = FakeApi([KESSOKU])
    monkeypatch.setattr(romanize.ArtistRomanizer, "_api", staticmethod(lambda: api))
    _two_kessoku_albums(env)
    # With folder.jpg as "clutter", beets deletes it while moving the last album out.
    config["clutter"] = ["folder.jpg"]
    client.post("/library/romanize", data={"only_new": "1", "write": "1", "move": "1"})
    run_jobs(app)
    assert (env["music"] / "kessoku band" / "folder.jpg").read_bytes() == b"\xff\xd8picture"
    assert not (env["music"] / "結束バンド").exists()
    assert app.state.artwork.artist_info("kessoku band")["image"] is not None


def test_artist_links_work_for_names_with_spaces(app, client, env, monkeypatch):
    api = FakeApi([KESSOKU])
    monkeypatch.setattr(romanize.ArtistRomanizer, "_api", staticmethod(lambda: api))
    _two_kessoku_albums(env)
    client.post("/library/romanize", data={"only_new": "1", "write": "1", "move": "1"})
    run_jobs(app)
    page = client.get("/library/artists").text
    link = "/library/artist?name=" + quote("kessoku band", safe="")
    assert link in page
    hits = client.get(link).text
    assert "Seisyun Complex" in hits and "Kessoku Band" in hits
    album = beets_ctx.lib().albums()[0]
    assert link in client.get(f"/library/album/{album.id}").text


def test_renaming_by_hand_keeps_the_picture(app, client, env):
    _two_kessoku_albums(env)
    client.post(
        "/library/artists/merge",
        data={"name": ["結束バンド"], "target": "Kessoku Band", "write": "1", "move": "1"},
    )
    run_jobs(app)
    assert (env["music"] / "Kessoku Band" / "folder.jpg").read_bytes() == b"\xff\xd8picture"
    assert not (env["music"] / "結束バンド").exists()


def test_missing_artist_picture_is_not_cached(client):
    r = client.get("/artwork/artist/image?name=nobody")
    assert r.headers["cache-control"] == "no-store"


def test_romanize_by_hand_starts_from_a_reading_and_can_be_undone(app, client, env, monkeypatch):
    """Yorushika's 二人称: no source romanizes it, so the album page offers it by hand."""
    lib = beets_ctx.lib()
    folder = make_album(env["inbox"] / "y", artist="Yorushika", album="二人称", n=2)
    key = importer.preview_folder(str(folder))[0]["key"]
    importer.apply_folder(str(folder), {key: {"action": "asis"}})
    album = lib.albums()[0]
    items = sorted(album.items(), key=lambda i: i.track)
    items[0].title = "火星人"
    items[0].store()
    # Another album already romanizes this circle: its name is offered as it is.
    other = make_album(env["inbox"] / "z", artist="ヨルシカ", album="Other", n=1)
    key = importer.preview_folder(str(other))[0]["key"]
    importer.apply_folder(str(other), {key: {"action": "asis"}})
    items[1].artist = "ヨルシカ"
    items[1].store()
    o = next(a for a in lib.albums() if a.album == "Other")
    o.albumartist, o.albumartist_original = "Yorushika", "ヨルシカ"
    o.store(inherit=False)

    assert "Romanize by hand" in client.get(f"/library/album/{album.id}").text
    rows = {r["key"]: r for r in romanize.by_hand(album, items, lib)}
    assert set(rows) == {"album", f"title-{items[0].id}", f"artist-{items[1].id}"}
    assert rows["album"]["guess"] and rows["album"]["value"] == "Ni ninshou"
    assert rows[f"artist-{items[1].id}"] == {
        "key": f"artist-{items[1].id}",
        "label": "Track 2 artist",
        "written": "ヨルシカ",
        "value": "Yorushika",
        "guess": False,
    }
    page = client.get(f"/library/album/{album.id}/romanize").text
    assert "Check the machine readings" in page and 'value="Ni ninshou"' in page

    r = client.post(
        f"/library/album/{album.id}/romanize/names",
        data={
            "album": "Nininshou",
            f"title-{items[0].id}": "Kaseijin",
            f"artist-{items[1].id}": "Yorushika",
            "write": "1",
        },
        follow_redirects=False,
    )
    job_id = int(r.headers["location"].rsplit("/", 1)[1])
    run_jobs(app)
    job = app.state.state.job(job_id)
    assert job["status"] == "done", job["log"]
    album = lib.get_album(album.id)
    assert (album.album, album.album_original) == ("Nininshou", "二人称")
    items = sorted(album.items(), key=lambda i: i.track)
    assert (items[0].title, items[0].title_original) == ("Kaseijin", "火星人")
    assert (items[1].artist, items[1].artist_original) == ("Yorushika", "ヨルシカ")
    assert MediaFile(str(items[0].filepath)).title_original == "火星人"
    # Romanized names can be corrected later: the page shows them, as written alongside.
    rows = {r["key"]: r for r in romanize.by_hand(album, items, lib)}
    assert rows["album"] == {**rows["album"], "written": "二人称", "value": "Nininshou"}
    assert not rows["album"]["guess"]

    client.post(f"/jobs/{job_id}/undo")
    run_jobs(app)
    album = lib.get_album(album.id)
    assert album.album == "二人称" and "album_original" not in album


def test_romanize_by_hand_with_nothing_to_do(app, client, env):
    folder = make_album(env["inbox"] / "l", artist="Latin", album="Latin")
    key = importer.preview_folder(str(folder))[0]["key"]
    importer.apply_folder(str(folder), {key: {"action": "asis"}})
    album = beets_ctx.lib().albums()[0]
    page = client.get(f"/library/album/{album.id}/romanize").text
    assert "in Latin script already" in page
    assert client.get("/library/album/999/romanize").status_code == 404

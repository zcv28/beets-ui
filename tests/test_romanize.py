"""Romanized main names with the originals kept alongside."""

from __future__ import annotations

from types import SimpleNamespace

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


def test_romanize_skips_other_sources_and_missing_romaji(app, client, env, pair):
    lib = beets_ctx.lib()
    for name, source, mbid in (("A", "Discogs", "1"), ("B", "VocaDB", "none")):
        folder = make_album(env["inbox"] / name, artist=name, album=name)
        key = importer.preview_folder(str(folder))[0]["key"]
        importer.apply_folder(str(folder), {key: {"action": "asis"}})
        album = next(a for a in lib.albums() if a.album == name)
        album.data_source, album.mb_albumid = source, mbid
        album.store()
    client.post("/library/romanize", data={})
    run_jobs(app)
    log = last_log(app)
    assert "B - B: VocaDB has no romanized names" in log
    assert "Romanized 0 album(s); 1 without romanized names at the source, 1 from other" in log
    assert pair == [("VocaDB", "none")]


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

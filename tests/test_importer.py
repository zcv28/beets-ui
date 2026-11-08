from __future__ import annotations

from beets_ui import beets_ctx, importer

from conftest import make_album


def test_preview_captures_candidates_without_importing(env, fake_source):
    folder = make_album(env["inbox"] / "Test Artist - Test Album")
    tasks = importer.preview_folder(str(folder))
    assert len(tasks) == 1
    t = tasks[0]
    assert t["cur_artist"] == "Test Artist"
    assert [c["id"] for c in t["candidates"]][0] == "fake-1"
    assert t["candidates"][0]["match"] > t["candidates"][1]["match"]
    assert len(t["candidates"][0]["tracks"]) == 3
    assert len(beets_ctx.lib().albums()) == 0


def test_apply_imports_chosen_candidate(env, fake_source):
    folder = make_album(env["inbox"] / "a")
    tasks = importer.preview_folder(str(folder))
    key = tasks[0]["key"]
    out = importer.apply_folder(
        str(folder), {key: {"action": "apply", "id": "fake-2", "source": "Fake"}}
    )
    assert out[key].result == "imported"
    albums = list(beets_ctx.lib().albums())
    assert len(albums) == 1
    assert albums[0].album == "Other Album"
    assert len(list(albums[0].items())) == 3
    # copy mode: source still there, destination in the library dir
    assert folder.exists()
    assert str(env["music"]) in beets_ctx.lib().items()[0].filepath.as_posix()


def test_asis_and_skip(env, fake_source):
    folder = make_album(env["inbox"] / "b", album="Raw Album")
    key = importer.preview_folder(str(folder))[0]["key"]
    out = importer.apply_folder(str(folder), {key: {"action": "skip"}})
    assert out[key].result == "skipped"
    assert len(beets_ctx.lib().albums()) == 0
    out = importer.apply_folder(str(folder), {key: {"action": "asis"}})
    assert out[key].result == "imported"
    assert beets_ctx.lib().albums()[0].album == "Raw Album"


def test_duplicate_flagged_then_resolved(env, fake_source):
    first = make_album(env["inbox"] / "first")
    key1 = importer.preview_folder(str(first))[0]["key"]
    importer.apply_folder(str(first), {key1: {"action": "apply", "id": "fake-1", "source": "Fake"}})

    second = make_album(env["inbox"] / "second")
    key2 = importer.preview_folder(str(second))[0]["key"]
    dec = {"action": "apply", "id": "fake-1", "source": "Fake"}
    out = importer.apply_folder(str(second), {key2: dec})
    assert out[key2].result == "duplicate"
    assert out[key2].duplicates[0]["album"] == "Test Album"
    assert len(beets_ctx.lib().albums()) == 1

    out = importer.apply_folder(str(second), {key2: {**dec, "duplicate": "remove"}})
    assert out[key2].result == "imported"
    assert len(beets_ctx.lib().albums()) == 1

    out = importer.apply_folder(str(second), {key2: {**dec, "duplicate": "keep"}})
    assert len(beets_ctx.lib().albums()) == 2


def test_config_overrides_are_removed(env, fake_source):
    from beets import config

    before = len(config.sources)
    folder = make_album(env["inbox"] / "c")
    importer.preview_folder(str(folder), mode="grouped")
    assert len(config.sources) == before
    assert not config["import"]["group_albums"].get(bool)

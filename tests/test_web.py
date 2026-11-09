from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from beets_ui import beets_ctx
from beets_ui.settings import Settings

from conftest import make_album


@pytest.fixture
def app(env, fake_source):
    from beets_ui.main import create_app

    settings = Settings.from_env()
    app = create_app(settings, background=False)
    return app


@pytest.fixture
def client(app):
    c = TestClient(app)
    r = c.post("/login", data={"password": "secret"}, follow_redirects=False)
    assert r.status_code == 303
    return c


def run_jobs(app):
    app.state.jobs.run_pending_sync()


def test_login_required(app):
    c = TestClient(app)
    r = c.get("/inbox", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert c.get("/healthz").json()["ok"]
    r = c.post("/login", data={"password": "nope"})
    assert r.status_code == 401


def test_cross_origin_post_rejected(client):
    r = client.post("/inbox/scan", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_inbox_review_and_import_flow(app, client, env):
    make_album(env["inbox"] / "Test Artist - Test Album")
    r = client.post("/inbox/scan", follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/inbox")
    assert "Test Artist - Test Album" in page.text

    f = "Test Artist - Test Album"
    client.post("/inbox/folder/preview", data={"f": f})
    run_jobs(app)
    row = app.state.state.folder(f)
    assert row["status"] == "ready", row
    page = client.get(f"/inbox/folder?f={quote(f)}")
    assert page.status_code == 200
    assert "Other Album" in page.text and "Import" in page.text

    # choose the second candidate
    client.post("/inbox/folder/import", data={"f": f, "choice-0": "c:1"})
    run_jobs(app)
    row = app.state.state.folder(f)
    assert row["status"] == "imported", row
    albums = list(beets_ctx.lib().albums())
    assert [a.album for a in albums] == ["Other Album"]

    # library pages render
    assert "Other Album" in client.get("/library").text
    assert "Other Album" in client.get("/library?view=table").text
    aid = albums[0].id
    assert client.get(f"/library/album/{aid}").status_code == 200
    assert client.get(f"/library/album/{aid}/cover").headers["content-type"].startswith("image/")
    assert client.get("/library/artists").status_code == 200
    assert client.get("/library/tracks").status_code == 200
    item = albums[0].items()[0]
    assert client.get(f"/library/item/{item.id}").status_code == 200

    # edit
    client.post(f"/library/album/{aid}/edit", data={"album": "Renamed", "extra": "mood=calm"})
    run_jobs(app)
    a = beets_ctx.lib().get_album(aid)
    assert a.album == "Renamed" and a["mood"] == "calm"

    # delete source folder from inbox
    client.post("/inbox/folder/delete", data={"f": f})
    assert not (env["inbox"] / f).exists()

    # remove from library
    client.post(f"/library/album/{aid}/remove", data={})
    run_jobs(app)
    assert len(beets_ctx.lib().albums()) == 0


def test_duplicate_flow_via_web(app, client, env):
    for name in ("one", "two"):
        make_album(env["inbox"] / name)
    client.post("/inbox/scan")
    for name in ("one", "two"):
        client.post("/inbox/folder/preview", data={"f": name})
    run_jobs(app)
    client.post("/inbox/folder/import", data={"f": "one", "choice-0": "c:0"})
    run_jobs(app)
    client.post("/inbox/folder/import", data={"f": "two", "choice-0": "c:0"})
    run_jobs(app)
    assert app.state.state.folder("two")["status"] == "duplicate"
    page = client.get("/inbox/folder?f=two")
    assert "Already in your library" in page.text
    client.post("/inbox/folder/import", data={"f": "two", "choice-0": "c:0", "dup-0": "keep"})
    run_jobs(app)
    assert app.state.state.folder("two")["status"] == "imported"
    assert len(beets_ctx.lib().albums()) == 2


def test_pages_render(client):
    for url in ("/inbox", "/library", "/console", "/jobs", "/artwork", "/artwork/artists"):
        r = client.get(url)
        assert r.status_code == 200, url
    # soulseek disabled without config
    assert client.get("/soulseek").status_code == 404


def test_console_job(app, client):
    r = client.post("/console", data={"cmd": "beet version"}, follow_redirects=False)
    assert r.status_code == 303
    job_id = int(r.headers["location"].rsplit("/", 1)[1])
    run_jobs(app)
    job = app.state.state.job(job_id)
    assert job["status"] == "done", job["log"]
    assert "beets version" in job["log"]
    assert client.get(f"/jobs/{job_id}").status_code == 200


def test_console_rejects_interactive(client):
    r = client.post("/console", data={"cmd": "config -e"})
    assert "interactive" in r.text


def test_theme_setting_and_bundled_assets(env, fake_source, monkeypatch):
    from beets_ui.main import create_app
    from beets_ui.settings import SettingsError

    monkeypatch.setenv("BEETSUI_THEME", "phosphor")
    app = create_app(Settings.from_env(), background=False)
    c = TestClient(app)
    login = c.get("/login")
    assert 'data-theme="phosphor"' in login.text and "Log In" in login.text
    c.post("/login", data={"password": "secret"})
    page = c.get("/inbox").text
    assert 'data-theme="phosphor"' in page and 'class="nc-window app-window"' in page
    for path in (
        "/static/nc/tokens.css",
        "/static/nc/bundle.css",
        "/static/fonts/fonts.css",
        "/static/fonts/arimo-latin.woff2",
        "/static/app.css",
        "/static/app.js",
    ):
        assert c.get(path).status_code == 200, path
    assert "fonts.googleapis" not in page
    monkeypatch.setenv("BEETSUI_THEME", "neon")
    with pytest.raises(SettingsError):
        Settings.from_env()

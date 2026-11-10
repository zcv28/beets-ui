"""App-like navigation: boosted pages, in-place actions with pop-ups, live updates, install."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from beets_ui.settings import Settings

from conftest import make_album

BOOSTED = {"HX-Request": "true", "HX-Boosted": "true"}
FRAGMENT = {"HX-Request": "true"}


@pytest.fixture
def app(env, fake_source):
    from beets_ui.main import create_app

    return create_app(Settings.from_env(), background=False)


@pytest.fixture
def client(app):
    c = TestClient(app)
    assert c.post("/login", data={"password": "secret"}, follow_redirects=False).status_code == 303
    return c


def test_shell_boosts_and_keeps_player_and_toasts(client):
    page = client.get("/inbox").text
    assert '<body hx-boost="true">' in page
    assert 'id="player-bar"' in page and 'id="toasts"' in page
    assert page.count("hx-preserve") >= 2
    assert 'rel="manifest"' in page and 'name="theme-color"' in page
    # Logging out reloads the whole page.
    assert 'action="/logout" class="sidebar-user" hx-boost="false"' in page


def test_login_page_is_not_boosted(app):
    page = TestClient(app).get("/login").text
    assert "hx-boost" not in page


def test_boosted_paging_gets_the_whole_page(client):
    whole = client.get("/library?page=2", headers=BOOSTED).text
    assert "<html" in whole
    rows = client.get("/library?page=2&view=table", headers=FRAGMENT).text
    assert "<html" not in rows


def test_inbox_delete_in_place(client, app, env):
    make_album(env["inbox"] / "Gone")
    app.state.inbox.scan()
    r = client.post("/inbox/folder/delete", data={"f": "Gone"}, headers=FRAGMENT)
    assert r.status_code == 200
    detail = json.loads(r.headers["HX-Trigger"])["toast"]
    assert detail["title"] == "Deleted Gone" and detail["kind"] == "success"
    assert not (env["inbox"] / "Gone").exists()

    make_album(env["inbox"] / "Busy")
    app.state.inbox.scan()
    app.state.state.upsert_folder("Busy", status="importing")
    r = client.post("/inbox/folder/delete", data={"f": "Busy"}, headers=FRAGMENT)
    assert r.status_code == 409
    assert json.loads(r.headers["HX-Trigger"])["toast"]["kind"] == "destructive"
    assert (env["inbox"] / "Busy").exists()


def test_inbox_delete_from_a_boosted_form_goes_back_to_the_list(client, env, app):
    make_album(env["inbox"] / "Plain")
    app.state.inbox.scan()
    r = client.post(
        "/inbox/folder/delete",
        data={"f": "Plain", "back": "/inbox"},
        headers=BOOSTED,
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/inbox"


def test_dismiss_offers_undo(client, app):
    r = client.post(
        "/insights/gaps/dismiss",
        data={"source": "vocadb", "release_id": "7", "label": "Some EP"},
        headers=FRAGMENT,
    )
    detail = json.loads(r.headers["HX-Trigger"])["toast"]
    assert detail["title"] == "Dismissed Some EP"
    assert detail["undo"] == {
        "url": "/insights/gaps/restore",
        "vals": {"source": "vocadb", "release_id": "7"},
    }
    assert app.state.state.dismissed("release")
    client.post(detail["undo"]["url"], data=detail["undo"]["vals"], headers=FRAGMENT)
    assert not app.state.state.dismissed("release")

    r = client.post(
        "/insights/upgrades/dismiss",
        data={"album_id": "3", "reason": "lossy", "label": "Album"},
        headers=FRAGMENT,
    )
    undo = json.loads(r.headers["HX-Trigger"])["toast"]["undo"]
    client.post("/insights/upgrades/dismiss", data={"album_id": "4", "reason": "lossy"})
    client.post(undo["url"], data=undo["vals"], headers=FRAGMENT)
    # Undo lists that one album again; the other stays dismissed.
    assert len(app.state.state.dismissed("upgrade")) == 1


def test_jobs_version_changes(app):
    state = app.state.state
    before = state.jobs_version()
    app.state.jobs.submit("console", "Run something", {"cmd": "version"})
    assert state.jobs_version() != before


def test_event_stream_announces_every_topic_on_reconnect(client, monkeypatch):
    from beets_ui.routes import events

    monkeypatch.setattr(events, "LIFETIME", 0.5)
    monkeypatch.setattr(events, "TICK", 0.1)
    with client.stream("GET", "/events", headers={"Last-Event-ID": "1"}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert body.startswith("retry: ")
    for topic in ("jobs", "inbox", "downloads"):
        assert f"event: {topic}\n" in body


def test_event_stream_is_quiet_until_something_changes(client, monkeypatch):
    from beets_ui.routes import events

    monkeypatch.setattr(events, "LIFETIME", 0.3)
    monkeypatch.setattr(events, "TICK", 0.1)
    with client.stream("GET", "/events") as r:
        body = "".join(r.iter_text())
    assert "event:" not in body


def test_install_files(client):
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    manifest = TestClient(client.app).get("/static/manifest.webmanifest")
    assert manifest.status_code == 200
    data = json.loads(manifest.text)
    assert data["display"] == "standalone"
    assert any(i.get("purpose") == "maskable" for i in data["icons"])
    for icon in data["icons"]:
        assert TestClient(client.app).get(icon["src"]).status_code == 200


def test_live_regions_are_marked(client):
    assert 'data-refresh="jobs"' in client.get("/jobs").text
    page = client.get("/inbox").text
    assert 'data-refresh="inbox"' in page and 'data-refresh="jobs downloads wanted"' in page

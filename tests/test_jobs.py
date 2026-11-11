"""Jobs: dismissing finished jobs, failed ones too, from the list, a job's page and Home."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from beets_ui.settings import Settings

HX = {"HX-Request": "true"}


@pytest.fixture
def app(env, fake_source):
    from beets_ui.main import create_app

    return create_app(Settings.from_env(), background=False)


@pytest.fixture
def client(app):
    c = TestClient(app)
    assert c.post("/login", data={"password": "secret"}, follow_redirects=False).status_code == 303
    return c


def job(state, title, status, error=None):
    jid = state.create_job("console", title, {})
    now = time.time()
    state.set_job(jid, status=status, error=error, started_at=now - 1, finished_at=now)
    return jid


def test_dismissing_a_failed_job_hides_it_from_home_and_the_list(app, client):
    state = app.state.state
    failed = job(state, "Broken import", "failed", "boom")
    ok = job(state, "Fine import", "done")
    home = client.get("/").text
    assert "Job failed: Broken import" in home and f'hx-post="/jobs/{failed}/dismiss"' in home
    version = state.jobs_version()

    r = client.post(f"/jobs/{failed}/dismiss", headers=HX)
    assert r.status_code == 200
    toast = json.loads(r.headers["HX-Trigger"])["toast"]
    assert toast["title"] == "Dismissed Broken import"
    assert toast["undo"]["url"] == f"/jobs/{failed}/restore"
    assert state.jobs_version() != version  # open pages refresh themselves

    assert "Broken import" not in client.get("/").text
    page = client.get("/jobs").text
    assert "Broken import" not in page and "Fine import" in page
    assert 'href="/jobs?dismissed=true">1 dismissed' in page
    page = client.get("/jobs?dismissed=true").text
    assert "Broken import" in page and "Fine import" not in page
    assert "Dismissed" in client.get(f"/jobs/{failed}").text  # its page and log stay

    # Undo from the pop-up.
    client.post(f"/jobs/{failed}/restore", headers=HX)
    assert "Job failed: Broken import" in client.get("/").text
    assert state.job(ok)["dismissed_at"] is None


def test_dismiss_all_finished_leaves_running_jobs(app, client):
    state = app.state.state
    job(state, "Old one", "done")
    job(state, "Failed one", "failed", "boom")
    running = job(state, "Still going", "running")
    r = client.post("/jobs/dismiss", follow_redirects=False)
    assert r.headers["location"] == "/jobs"
    page = client.get("/jobs").text
    assert "Dismissed 2 finished jobs." in page
    assert "Still going" in page and "Old one" not in page and "Failed one" not in page
    # A running job can't be dismissed.
    r = client.post(f"/jobs/{running}/dismiss", headers=HX)
    assert json.loads(r.headers["HX-Trigger"])["toast"]["kind"] == "destructive"
    assert state.job(running)["dismissed_at"] is None
    client.post("/jobs/restore")
    assert "Old one" in client.get("/jobs").text


def test_dismiss_from_the_job_page(app, client):
    state = app.state.state
    jid = job(state, "Broken import", "failed", "boom")
    r = client.post(f"/jobs/{jid}/dismiss", follow_redirects=False)
    assert r.headers["location"] == f"/jobs/{jid}"
    page = client.get(f"/jobs/{jid}").text
    assert "it no longer shows in the jobs list or on Home" in page
    assert f'action="/jobs/{jid}/restore"' in page

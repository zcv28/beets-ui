"""Home: what needs you, what is coming in, what is missing, and how the library is doing.

Everything here is a cheap query or cached state, so the page opens at once on
a large library. The left column refreshes itself with live updates.
"""

from __future__ import annotations

import time
from urllib.parse import quote

from fastapi import APIRouter, Request

from .. import beets_ctx, discography, upgrades
from .. import library as libsvc
from ..sources import incoming, requests_in_flight
from ..web import partial, render
from .inbox import REVIEW, _queue

router = APIRouter()

COMING = ("queued", "searching", "downloading", "arrived", "lookup", "importing")
RECENT_FAILURES = 86400  # failed jobs show for a day


def _now(app) -> dict:
    """The left column: what needs you, and what is on its way."""
    state = app.state.state
    folders = state.folders()
    queue = _queue(folders, "")
    requests = requests_in_flight(app)
    downloads = incoming(app)
    needs = []
    watch = getattr(app.state, "source_watch", None)
    status = watch.status if watch else None
    if status and not status.get("connected"):
        label = getattr(app.state.source, "label", "The download source")
        reachable = status.get("reachable")
        needs.append(
            {
                "tone": "destructive" if not reachable else "warning",
                "title": f"Can't reach {label}" if not reachable else "Downloads are paused",
                "detail": "Get can't search or download until it's back."
                if not reachable
                else f"{label} isn't connected.",
                "href": "/get/coming",
                "action": "Open",
            }
        )
    if queue["total"]:
        needs.append(
            {
                "tone": "warning",
                "title": f"{queue['total']} folder{'s' if queue['total'] != 1 else ''} to review",
                "detail": "Looked up and waiting for your decision",
                "href": f"/inbox/folder?f={quote(queue['next'], safe='')}"
                if queue["next"]
                else "/inbox",
                "action": "Review",
                "path": queue["next"],
            }
        )
    unlooked = sum(1 for f in folders if f["status"] == "new")
    if unlooked and not app.state.settings.auto_preview:
        needs.append(
            {
                "tone": "warning",
                "title": f"{unlooked} folder{'s' if unlooked != 1 else ''} not looked up",
                "detail": "Look them up to review them",
                "href": "/inbox?status=new",
                "action": "Open",
            }
        )
    for w in requests:
        if w["status"] == "choosing":
            needs.append(
                {
                    "tone": "warning",
                    "title": f"Choose a copy of {w['album']}",
                    "detail": w["status_text"],
                    "href": f"/wanted/{w['id']}",
                    "action": "Choose",
                    "name": w["album"],
                }
            )
        elif w["status"] == "failed":
            needs.append(
                {
                    "tone": "destructive",
                    "title": f"Couldn't get {w['album']}",
                    "detail": w["status_text"],
                    "href": f"/wanted/{w['id']}",
                    "action": "Open",
                    "name": w["album"],
                }
            )
    for d in downloads:
        if d["state"] == "failed":
            needs.append(
                {
                    "tone": "destructive",
                    "title": f"Download failed: {d['folder_name']}",
                    "detail": d["label"],
                    "href": "/get/coming",
                    "action": "Retry",
                    "name": d["folder_name"],
                }
            )
    since = time.time() - RECENT_FAILURES
    for j in state.jobs(30, dismissed=False):
        if j["status"] == "failed" and (j["finished_at"] or 0) > since and j["kind"] != "preview":
            needs.append(
                {
                    "tone": "destructive",
                    "title": f"Job failed: {j['title']}",
                    "detail": (j["error"] or "")[:140],
                    "href": f"/jobs/{j['id']}",
                    "action": "Open",
                    "dismiss": f"/jobs/{j['id']}/dismiss",
                }
            )
    coming = [w for w in requests if w["status"] in COMING]
    return {
        "needs": needs,
        "coming": coming,
        "downloads": [d for d in downloads if d["state"] in ("downloading", "queued")],
        "running": len(app.state.jobs.running()),
        "pending": app.state.jobs.pending(),
        "review_states": REVIEW,
        # The column also refreshes on its own, outside the full page's context.
        "sources_enabled": getattr(app.state, "source", None) is not None,
    }


def _care(lib, state) -> list[dict]:
    """Library upkeep, in counts that link to where you fix them."""
    with lib.transaction() as tx:
        no_cover = tx.query("SELECT COUNT(*) FROM albums WHERE artpath IS NULL OR artpath = ''")[0][
            0
        ]
        tracks, synced = tx.query(
            "SELECT COUNT(*), COALESCE(SUM(lyrics LIKE '[%:%]%'), 0) FROM items"
        )[0]
    counts = upgrades.find(lib, state)["counts"]
    rows = []

    def albums(n: int) -> str:
        return "album" if n == 1 else "albums"

    if no_cover:
        rows.append(("No cover file", no_cover, albums(no_cover), "/artwork?filter=nofile"))
    for key, label in (
        ("lossy", "Lossy"),
        ("bitrate", "Low bitrate"),
        ("incomplete", "Incomplete"),
    ):
        if counts.get(key):
            n = counts[key]
            rows.append((label, n, albums(n), f"/insights/upgrades?reason={key}"))
    if tracks:
        rows.append(
            ("Synced lyrics", f"{round(100 * synced / tracks)}%", "of tracks", "/insights/lyrics")
        )
    return [{"label": a, "count": b, "unit": c, "href": d} for a, b, c, d in rows]


def _missing(lib, state, wanted) -> dict:
    rows = [r for r in discography.gaps(lib, state) if r["looked_up"]]
    with_gaps = sorted((r for r in rows if r["missing"]), key=lambda r: -len(r["missing"]))
    open_ids = {
        (w["source"], w["release_id"])
        for w in wanted
        if w["kind"] == "missing" and w["status"] not in ("cancelled", "failed")
    }
    for r in with_gaps:
        r["gettable"] = sum(1 for rel in r["missing"] if (r["source"], rel["id"]) not in open_ids)
    return {
        "looked": len(rows),
        "artists": len(with_gaps),
        "releases": sum(len(r["missing"]) for r in with_gaps),
        "top": with_gaps[:6],
        "groups": len(discography.library_groups(lib)),
    }


@router.get("/")
def home(request: Request):
    app = request.app
    state = app.state.state
    lib = beets_ctx.lib()
    recent, _ = libsvc.albums("", "added", per_page=12)
    return render(
        request,
        "home.html",
        nav="home",
        **_now(app),
        missing=_missing(lib, state, state.wanted_list()),
        care=_care(lib, state),
        recent=recent,
        stats=libsvc.stats(),
        get_enabled=getattr(app.state, "acquirer", None) is not None,
    )


@router.get("/home/now")
def home_now(request: Request):
    return partial(request, "home/_now.html", **_now(request.app))

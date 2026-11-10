"""Get: what is missing from the library, what is coming in, and finding music by hand.

Missing (discography gaps) used to live under Insights, and Coming in and Find
under the Inbox; their old addresses redirect here. The Inbox is for review.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import beets_ctx, discography, discover
from ..sources import incoming, requests_in_flight
from ..web import flash, is_fragment, partial, redirect, render, toast, user

router = APIRouter(prefix="/get")
legacy = APIRouter()


def _done(request: Request, back: str, fallback: str, done=None):
    """For an in-place action the row removes itself (``done`` is its pop-up);
    otherwise back to the page."""
    if is_fragment(request):
        return done or HTMLResponse("")
    ok = back.startswith("/") and not back.startswith("//")
    return redirect(back if ok else fallback)


@router.get("")
def index():
    return redirect("/get/missing")


# -------------------------------------------------------------- coming in


@router.get("/coming")
def coming_page(request: Request):
    return render(
        request,
        "get/coming.html",
        nav="get",
        requests=requests_in_flight(request.app),
        downloads=incoming(request.app),
    )


@router.get("/requests")
def coming_requests(request: Request):
    return partial(request, "get/_requests.html", requests=requests_in_flight(request.app))


# --------------------------------------------------------------- discover


def _picked(kinds: list[str]) -> tuple[str, ...]:
    picked = tuple(k for k in kinds if k in discography.KINDS)
    return picked or discover.DEFAULT_KINDS


@router.get("/discover")
def discover_page(request: Request, kinds: list[str] = Query(default=[])):
    settings = request.app.state.settings
    state = request.app.state.state
    if settings.listenbrainz_user and discover.sync_due(state):
        request.app.state.jobs.submit(
            discover.SYNC_KIND, "Read ListenBrainz recommendations", {}, user(request)
        )
    jobs = state.jobs(1, kind=discover.SYNC_KIND)
    last = jobs[0] if jobs else None
    picked = _picked(kinds)
    return render(
        request,
        "get/discover.html",
        nav="get",
        enabled=bool(settings.listenbrainz_user),
        data=discover.view(beets_ctx.lib(), state, picked),
        kinds=discography.KINDS,
        picked=picked,
        busy=bool(last and last["status"] in ("queued", "running")),
        last_job=last,
        get_enabled=getattr(request.app.state, "acquirer", None) is not None,
    )


@router.get("/discover/busy")
def discover_busy(request: Request):
    """Polled while a sync runs: 204 until it is done, then the page reloads itself."""
    jobs = request.app.state.state.jobs(1, kind=discover.SYNC_KIND)
    if jobs and jobs[0]["status"] in ("queued", "running"):
        return Response(status_code=204)
    return Response(headers={"HX-Refresh": "true"})


@router.post("/discover/sync")
def discover_sync(request: Request):
    if not request.app.state.settings.listenbrainz_user:
        flash(request, "Set LISTENBRAINZ_USER to your ListenBrainz name first.", "warn")
        return redirect("/get/discover")
    request.app.state.jobs.submit(
        discover.SYNC_KIND, "Read ListenBrainz recommendations", {}, user(request)
    )
    return redirect("/get/discover")


@router.post("/discover/dismiss")
def discover_dismiss(
    request: Request, id: str = Form(...), label: str = Form(""), back: str = Form("")
):
    request.app.state.state.dismiss(discover.DISMISS_KIND, id, label)
    undo = {"url": "/get/discover/restore", "vals": {"id": id}}
    done = toast(f"Dismissed {label}" if label else "Dismissed", undo=undo)
    return _done(request, back, "/get/discover", done)


@router.post("/discover/restore")
def discover_restore(request: Request, id: str = Form(...)):
    request.app.state.state.undismiss(discover.DISMISS_KIND, id)
    return _done(request, "", "/get/discover")


@router.post("/discover/restore-all")
def discover_restore_all(request: Request):
    state = request.app.state.state
    for key in state.dismissed(discover.DISMISS_KIND):
        state.undismiss(discover.DISMISS_KIND, key)
    flash(request, "Dismissed albums are suggested again.")
    return redirect("/get/discover")


# ---------------------------------------------------------------- missing


@router.get("/missing")
def gaps_page(
    request: Request,
    kinds: list[str] = Query(default=[]),
    show: str = "gaps",
    sort: str = "missing",
):
    picked = tuple(k for k in (kinds or discography.DEFAULT_KINDS) if k in discography.KINDS)
    picked = picked or discography.DEFAULT_KINDS
    rows = discography.gaps(beets_ctx.lib(), request.app.state.state, picked)
    looked = [r for r in rows if r["looked_up"]]
    if show == "gaps":
        shown = [r for r in looked if r["missing"]]
    elif show == "complete":
        shown = [r for r in looked if not r["missing"]]
    else:
        shown = rows
    sort = sort if sort in ("missing", "name") else "missing"
    shown = sorted(shown, key=lambda r: r["artist"].lower())
    if sort == "missing":
        # Most missing first; artists not looked up yet go last.
        shown.sort(key=lambda r: (not r["looked_up"], -len(r["missing"])))
    return render(
        request,
        "get/missing.html",
        nav="get",
        rows=shown,
        artists=len(rows),
        looked=len(looked),
        missing=sum(len(r["missing"]) for r in looked),
        kinds=discography.KINDS,
        picked=picked,
        show=show,
        sort=sort,
        busy=_gaps_busy(request),
        wanted=_wanted_releases(request),
        get_enabled=getattr(request.app.state, "acquirer", None) is not None,
    )


def _wanted_releases(request: Request) -> dict[str, dict]:
    """Requests for missing releases, by "source:release id" (latest wins)."""
    rows = request.app.state.state.wanted_list()
    return {f"{w['source']}:{w['release_id']}": w for w in rows if w["kind"] == "missing"}


def _gaps_busy(request: Request) -> bool:
    jobs = request.app.state.state.jobs(5, kind="discography")
    return any(j["status"] in ("queued", "running") for j in jobs)


@router.get("/missing/busy")
def gaps_busy(request: Request):
    """Polled while a lookup runs: 204 until it is done, then the page reloads itself."""
    if _gaps_busy(request):
        return Response(status_code=204)
    return Response(headers={"HX-Refresh": "true"})


@router.post("/missing/refresh")
def gaps_refresh(request: Request, source: str = Form(""), artist_id: str = Form("")):
    if source and artist_id:
        params = {"keys": [[source, artist_id]]}
        title = f"Discography of {artist_id} ({source})"
        g = discography.library_groups(beets_ctx.lib()).get((source, artist_id))
        if g:
            title = f"Discography of {g['artist']}"
    else:
        params, title = {}, "Look up discographies"
    job = request.app.state.jobs.submit("discography", title, params, user(request))
    flash(request, f"{title} queued as job #{job}. It runs alongside imports.")
    return redirect("/get/missing")


@router.post("/missing/dismiss")
def gaps_dismiss(
    request: Request,
    source: str = Form(...),
    release_id: str = Form(...),
    label: str = Form(""),
    back: str = Form(""),
):
    request.app.state.state.dismiss("release", discography.dismiss_key(source, release_id), label)
    undo = {"url": "/get/missing/restore", "vals": {"source": source, "release_id": release_id}}
    done = toast(f"Dismissed {label}" if label else "Dismissed", undo=undo)
    return _done(request, back, "/get/missing", done)


@router.post("/missing/restore")
def gaps_restore(request: Request, source: str = Form(...), release_id: str = Form(...)):
    request.app.state.state.undismiss("release", discography.dismiss_key(source, release_id))
    return _done(request, "", "/get/missing?show=all")


# ------------------------------------------------------------- old addresses


@legacy.get("/insights/gaps{rest:path}", include_in_schema=False)
def old_gaps(request: Request, rest: str = ""):
    target = "/get/missing" + rest
    if request.url.query:
        target += "?" + request.url.query
    return RedirectResponse(target, status_code=307)


@legacy.get("/inbox/find{rest:path}", include_in_schema=False)
def old_find(request: Request, rest: str = ""):
    target = "/get/find" + rest
    if request.url.query:
        target += "?" + request.url.query
    return RedirectResponse(target, status_code=307)

"""Insights: discography gaps, upgrade candidates, listening stats, lyrics."""

from __future__ import annotations

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, Response

from .. import beets_ctx, discography, listening, lyrics, upgrades
from ..web import flash, is_fragment, redirect, render, toast, user

router = APIRouter(prefix="/insights")


def _done(request: Request, back: str, fallback: str, done=None):
    """For an in-place action the row removes itself (``done`` is its pop-up);
    otherwise back to the page."""
    if is_fragment(request):
        return done or HTMLResponse("")
    ok = back.startswith("/insights") and not back.startswith("//")
    return redirect(back if ok else fallback)


@router.get("")
def index():
    return redirect("/insights/gaps")


# ------------------------------------------------------------------- gaps


@router.get("/gaps")
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
        "insights/gaps.html",
        nav="insights",
        rows=shown,
        artists=len(rows),
        looked=len(looked),
        missing=sum(len(r["missing"]) for r in looked),
        kinds=discography.KINDS,
        picked=picked,
        show=show,
        sort=sort,
        busy=_gaps_busy(request),
    )


def _gaps_busy(request: Request) -> bool:
    jobs = request.app.state.state.jobs(5, kind="discography")
    return any(j["status"] in ("queued", "running") for j in jobs)


@router.get("/gaps/busy")
def gaps_busy(request: Request):
    """Polled while a lookup runs: 204 until it is done, then the page reloads itself."""
    if _gaps_busy(request):
        return Response(status_code=204)
    return Response(headers={"HX-Refresh": "true"})


@router.post("/gaps/refresh")
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
    return redirect("/insights/gaps")


@router.post("/gaps/dismiss")
def gaps_dismiss(
    request: Request,
    source: str = Form(...),
    release_id: str = Form(...),
    label: str = Form(""),
    back: str = Form(""),
):
    request.app.state.state.dismiss("release", discography.dismiss_key(source, release_id), label)
    undo = {"url": "/insights/gaps/restore", "vals": {"source": source, "release_id": release_id}}
    done = toast(f"Dismissed {label}" if label else "Dismissed", undo=undo)
    return _done(request, back, "/insights/gaps", done)


@router.post("/gaps/restore")
def gaps_restore(request: Request, source: str = Form(...), release_id: str = Form(...)):
    request.app.state.state.undismiss("release", discography.dismiss_key(source, release_id))
    return _done(request, "", "/insights/gaps?show=all")


# --------------------------------------------------------------- upgrades


@router.get("/upgrades")
def upgrades_page(request: Request, reason: str = ""):
    reason = reason if reason in upgrades.REASONS else ""
    data = upgrades.find(beets_ctx.lib(), request.app.state.state, reason)
    return render(
        request,
        "insights/upgrades.html",
        nav="insights",
        reason=reason,
        reasons=upgrades.REASONS,
        min_px=request.app.state.settings.min_cover_px,
        **data,
    )


@router.post("/upgrades/dismiss")
def upgrades_dismiss(
    request: Request,
    album_id: int = Form(...),
    reason: str = Form(...),
    label: str = Form(""),
    back: str = Form(""),
):
    request.app.state.state.dismiss("upgrade", upgrades.dismiss_key(album_id, reason), label)
    undo = {"url": "/insights/upgrades/restore", "vals": {"album_id": album_id, "reason": reason}}
    done = toast(f"Dismissed {label}" if label else "Dismissed", undo=undo)
    return _done(request, back, "/insights/upgrades", done)


@router.post("/upgrades/restore")
def upgrades_restore(request: Request, album_id: int = Form(0), reason: str = Form("")):
    """List one dismissed album again (the pop-up's Undo), or all of them."""
    state = request.app.state.state
    if album_id and reason:
        state.undismiss("upgrade", upgrades.dismiss_key(album_id, reason))
        return _done(request, "", "/insights/upgrades")
    for key in state.dismissed("upgrade"):
        state.undismiss("upgrade", key)
    flash(request, "Dismissed albums are listed again.")
    return redirect("/insights/upgrades")


# -------------------------------------------------------------- listening


@router.get("/listening")
def listening_page(request: Request):
    settings = request.app.state.settings
    state = request.app.state.state
    if settings.jellyfin_enabled and listening.sync_due(state):
        request.app.state.jobs.submit(
            listening.SYNC_KIND, "Read Jellyfin play counts", {}, user(request)
        )
    synced = state.meta("jellyfin_sync")
    data = listening.stats(beets_ctx.lib(), state) if synced else None
    jobs = state.jobs(1, kind=listening.SYNC_KIND)
    return render(
        request,
        "insights/listening.html",
        nav="insights",
        enabled=settings.jellyfin_enabled,
        synced=synced,
        data=data,
        last_job=jobs[0] if jobs else None,
    )


@router.post("/listening/sync")
def listening_sync(request: Request):
    if not request.app.state.settings.jellyfin_enabled:
        flash(request, "Set JELLYFIN_URL and JELLYFIN_API_KEY first.", "wip")
        return redirect("/insights/listening")
    job = request.app.state.jobs.submit(
        listening.SYNC_KIND, "Read Jellyfin play counts", {}, user(request)
    )
    flash(request, f"Reading play counts from Jellyfin (job #{job}).")
    return redirect("/insights/listening")


@router.get("/listening/random")
def listening_random(request: Request):
    album_id = listening.random_unplayed(beets_ctx.lib(), request.app.state.state)
    if album_id is None:
        flash(request, "Every album has been played at least once.", "msg")
        return redirect("/insights/listening")
    return redirect(f"/library/album/{album_id}")


# ----------------------------------------------------------------- lyrics


@router.get("/lyrics")
def lyrics_page(request: Request):
    return render(
        request,
        "insights/lyrics.html",
        nav="insights",
        cov=lyrics.coverage(beets_ctx.lib()),
        mode=request.app.state.settings.lyrics_romaji,
        limit=lyrics.BULK_LIMIT,
        jobs=request.app.state.state.jobs(5, kind="lyrics"),
    )


@router.post("/lyrics/run")
def lyrics_run(
    request: Request,
    replace_plain: str = Form(""),
    force: str = Form(""),
    query: str = Form(""),
):
    params: dict = {"replace_plain": bool(replace_plain), "force": bool(force), "bulk": True}
    title = "Synced lyrics for the library"
    if query.strip():
        from .. import library as libsvc

        try:
            rows, _ = libsvc.items(query, per_page=5000)
        except libsvc.QueryError as e:
            flash(request, f"Bad query: {e}", "warn")
            return redirect("/insights/lyrics")
        if not rows:
            flash(request, "No tracks match that query.", "wip")
            return redirect("/insights/lyrics")
        params["item_ids"] = [i.id for i in rows]
        title = f"Synced lyrics for {query.strip()}"
    job = request.app.state.jobs.submit("lyrics", title, params, user(request))
    return redirect(f"/jobs/{job}")

"""Insights: upgrade candidates, listening stats, lyrics."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import beets_ctx, listening, lyrics, upgrades
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
    return redirect("/insights/upgrades")


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
        wanted={w["album_id"]: w for w in request.app.state.state.wanted_list() if w["album_id"]},
        get_enabled=getattr(request.app.state, "acquirer", None) is not None,
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
    # One reason, or several separated by commas ("Keep this copy" on every reason shown).
    for r in reason.split(","):
        if r in upgrades.REASONS:
            request.app.state.state.dismiss("upgrade", upgrades.dismiss_key(album_id, r), label)
    undo = {"url": "/insights/upgrades/restore", "vals": {"album_id": album_id, "reason": reason}}
    done = toast(f"Dismissed {label}" if label else "Dismissed", undo=undo)
    return _done(request, back, "/insights/upgrades", done)


@router.post("/upgrades/restore")
def upgrades_restore(request: Request, album_id: int = Form(0), reason: str = Form("")):
    """List one dismissed album again (the pop-up's Undo), or all of them."""
    state = request.app.state.state
    if album_id and reason:
        for r in reason.split(","):
            state.undismiss("upgrade", upgrades.dismiss_key(album_id, r))
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

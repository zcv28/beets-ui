"""Get: ask beets-ui to find, download and import an album (see acquire.py)."""

from __future__ import annotations

import contextlib

from fastapi import APIRouter, Form, HTTPException, Request

from .. import acquire, beets_ctx, discography
from ..sources import SourceError
from ..web import flash, is_fragment, partial, redirect, render, toast

router = APIRouter(prefix="/wanted")


def _engine(request: Request):
    acq = getattr(request.app.state, "acquirer", None)
    if acq is None:
        raise HTTPException(404, "Get needs a download source")
    return acq


def _wanted_or_404(request: Request, wanted_id: int) -> dict:
    w = request.app.state.state.wanted(wanted_id)
    if w is None:
        raise HTTPException(404, "request not found")
    return w


def _back(back: str, fallback: str) -> str:
    return back if back.startswith("/") and not back.startswith("//") else fallback


def _chip(request: Request, w: dict, title: str, text: str = ""):
    """In place: the request's state where its Get button was, and a pop-up."""
    resp = partial(request, "wanted/_chip.html", w=w)
    resp.headers["HX-Trigger"] = toast(title, text).headers["HX-Trigger"]
    return resp


def _gap_row(request: Request, source: str, artist_id: str, kinds: tuple[str, ...]) -> dict:
    rows = discography.gaps(beets_ctx.lib(), request.app.state.state, kinds)
    row = next((r for r in rows if r["source"] == source and r["artist_id"] == artist_id), None)
    if row is None:
        raise HTTPException(404, "artist not found in the discography lookups")
    return row


@router.post("/release")
def get_release(
    request: Request,
    source: str = Form(...),
    artist_id: str = Form(...),
    release_id: str = Form(...),
    back: str = Form("/get/missing"),
):
    """Get one missing release."""
    acq = _engine(request)
    row = _gap_row(request, source, artist_id, discography.KINDS)
    rel = next((x for x in row["missing"] + row["upcoming"] if x["id"] == release_id), None)
    if rel is None:
        raise HTTPException(404, "that release is not missing")
    wid, created = acquire.from_gap(request.app.state.state, row, rel)
    acq.wake()
    w = request.app.state.state.wanted(wid)
    title = f"Getting {rel['title']}" if created else f"Already getting {rel['title']}"
    if is_fragment(request):
        return _chip(request, w, title, "Follow it under Coming in.")
    flash(request, f"{title}. Follow it under Coming in.", "ok")
    return redirect(_back(back, "/get/missing"))


@router.post("/artist")
def get_artist(
    request: Request,
    source: str = Form(...),
    artist_id: str = Form(...),
    kinds: list[str] = Form([]),
    back: str = Form("/get/missing"),
):
    """Get every missing release of an artist (of the kinds shown)."""
    acq = _engine(request)
    picked = tuple(k for k in kinds if k in discography.KINDS) or discography.DEFAULT_KINDS
    row = _gap_row(request, source, artist_id, picked)
    new = sum(acquire.from_gap(request.app.state.state, row, rel)[1] for rel in row["missing"])
    acq.wake()
    if new:
        msg = f"Getting {new} album{'s' if new != 1 else ''} by {row['artist']}. Follow them under Coming in; two searches run at a time."
    else:
        msg = f"Every missing album by {row['artist']} is already being got."
    flash(request, msg, "ok")
    return redirect(_back(back, "/get/missing"))


@router.post("/discover")
def get_discovered(
    request: Request,
    id: str = Form(...),
    title: str = Form(...),
    artist: str = Form(...),
    date: str = Form(""),
    back: str = Form("/get/discover"),
):
    """Get an album from Discover: a MusicBrainz release group."""
    acq = _engine(request)
    row = {"source": "musicbrainz", "artist": artist, "artist_original": ""}
    rel = {"id": id, "title": title, "original": "", "date": date}
    wid, created = acquire.from_gap(request.app.state.state, row, rel)
    acq.wake()
    w = request.app.state.state.wanted(wid)
    msg = f"Getting {title}" if created else f"Already getting {title}"
    if is_fragment(request):
        return _chip(request, w, msg, "Follow it under Coming in.")
    flash(request, f"{msg}. Follow it under Coming in.", "ok")
    return redirect(_back(back, "/get/discover"))


@router.post("/album/{album_id}")
def get_better_copy(request: Request, album_id: int, back: str = Form("/insights/upgrades")):
    """Get a better copy of an album in the library; it replaces the old one."""
    acq = _engine(request)
    album = beets_ctx.lib().get_album(album_id)
    if album is None:
        raise HTTPException(404, "album not found")
    wid, created = acquire.from_album(request.app.state.state, album)
    acq.wake()
    w = request.app.state.state.wanted(wid)
    title = (
        f"Getting a better copy of {album.album}" if created else f"Already getting {album.album}"
    )
    if is_fragment(request):
        return _chip(request, w, title, "It replaces the album when it is imported.")
    flash(request, f"{title}. It replaces the album when it is imported.", "ok")
    return redirect(_back(back, "/insights/upgrades"))


@router.get("/{wanted_id}")
def request_page(request: Request, wanted_id: int):
    w = _wanted_or_404(request, wanted_id)
    folder = request.app.state.state.folder(w["folder"]) if w["folder"] else None
    return render(request, "wanted/request.html", nav="get", w=w, folder=folder)


@router.post("/{wanted_id}/choose")
def choose(request: Request, wanted_id: int, origin: str = Form(...), folder: str = Form(...)):
    acq = _engine(request)
    w = _wanted_or_404(request, wanted_id)
    try:
        ok = acq.choose(w, origin, folder)
    except SourceError as e:
        flash(request, str(e), "warn")
        return redirect(f"/wanted/{wanted_id}")
    if not ok:
        flash(request, "That copy has no files to download any more. Choose another one.", "warn")
        return redirect(f"/wanted/{wanted_id}")
    flash(request, f"Downloading {w['album']} from {origin}.", "ok")
    return redirect("/get/coming")


@router.post("/{wanted_id}/retry")
def retry(request: Request, wanted_id: int):
    """Start again from the first search, skipping copies that already failed."""
    acq = _engine(request)
    w = _wanted_or_404(request, wanted_id)
    request.app.state.state.update_wanted(
        wanted_id,
        status="queued",
        status_text="Waiting to search",
        query_idx=0,
        attempts=0,
        search_id="",
        error=None,
    )
    acq.wake()
    if is_fragment(request):
        return toast(f"Trying {w['album']} again")
    return redirect(f"/wanted/{wanted_id}")


@router.post("/{wanted_id}/cancel")
def cancel(request: Request, wanted_id: int):
    w = _wanted_or_404(request, wanted_id)
    source = request.app.state.source
    if w["status"] == "downloading" and source is not None:
        watch = request.app.state.source_watch
        rows = watch.rows if watch else []
        row = next(
            (r for r in rows if r["origin"] == w["origin"] and r["folder"] == w["remote_folder"]),
            None,
        )
        if row:
            with contextlib.suppress(SourceError):
                ids = [f["id"] for f in row["files"] if f["kind"] in ("queued", "active")]
                source.cancel(w["origin"], ids, remove=True)
    request.app.state.state.update_wanted(wanted_id, status="cancelled", status_text="Cancelled")
    if is_fragment(request):
        return toast(f"Cancelled {w['album']}")
    return redirect("/get/coming")


@router.post("/{wanted_id}/remove")
def remove(request: Request, wanted_id: int):
    w = _wanted_or_404(request, wanted_id)
    request.app.state.state.delete_wanted(wanted_id)
    if is_fragment(request):
        return toast(f"Removed {w['album']}")
    return redirect("/get/coming")

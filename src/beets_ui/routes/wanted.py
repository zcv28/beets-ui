"""Get: ask beets-ui to find, download and import an album (see acquire.py)."""

from __future__ import annotations

import contextlib

from fastapi import APIRouter, Form, HTTPException, Request

from .. import acquire, beets_ctx, discography, discover
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


@router.post("/discover-all")
def get_discovered_all(
    request: Request,
    section: str = Form(...),
    kinds: list[str] = Form([]),
    back: str = Form("/get/discover"),
):
    """Get every album of a Discover list (new releases of the kinds shown, or the
    suggestions) that isn't asked for yet."""
    acq = _engine(request)
    state = request.app.state.state
    picked = tuple(k for k in kinds if k in discography.KINDS) or discover.DEFAULT_KINDS
    data = discover.view(beets_ctx.lib(), state, picked)
    albums = {"released": data["released"], "suggested": data["albums"]}.get(section)
    if albums is None:
        raise HTTPException(400, "unknown list")
    new = 0
    for a in albums:
        if a["wanted"]:
            continue
        row = {"source": "musicbrainz", "artist": a["artist"], "artist_original": ""}
        rel = {
            "id": a["id"],
            "title": a["title"],
            "original": "",
            "date": a.get("date") or a.get("year") or "",
        }
        new += acquire.from_gap(state, row, rel)[1]
    acq.wake()
    what = "new release" if section == "released" else "suggested album"
    if new:
        flash(
            request,
            f"Getting {new} {what}{'s' if new != 1 else ''}. Follow them under Coming in; two searches run at a time.",
            "ok",
        )
    else:
        flash(request, f"Every {what} here is already being got.", "ok")
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
    state = request.app.state.state
    folder = state.folder(w["folder"]) if w["folder"] else None
    searches = state.searches_for_wanted(wanted_id)[:30]
    return render(request, "wanted/request.html", nav="get", w=w, folder=folder, searches=searches)


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


def _restart(state, w: dict) -> None:
    """From the first search again, skipping copies that already failed."""
    state.update_wanted(
        w["id"],
        status="queued",
        status_text="Waiting to search",
        query_idx=0,
        attempts=0,
        researched=0,
        search_id="",
        error=None,
    )
    acquire.note(state, w, "You asked it to keep trying")


@router.post("/retry-all")
def retry_all(request: Request):
    """Search again for every request that couldn't get its album."""
    acq = _engine(request)
    state = request.app.state.state
    failed = state.wanted_list(("failed",))
    for w in failed:
        _restart(state, w)
    acq.wake()
    n = len(failed)
    title = f"Searching again for {n} album{'s' if n != 1 else ''}"
    if is_fragment(request):
        return toast(title, "From the start, skipping copies that failed. Two search at a time.")
    flash(request, f"{title}.", "ok")
    return redirect("/get/coming")


@router.post("/{wanted_id}/retry")
def retry(request: Request, wanted_id: int):
    """Start again from the first search, skipping copies that already failed."""
    acq = _engine(request)
    w = _wanted_or_404(request, wanted_id)
    _restart(request.app.state.state, w)
    acq.wake()
    if is_fragment(request):
        return toast(
            f"Searching again for {w['album']}", "From the start, skipping copies that failed."
        )
    return redirect(f"/wanted/{wanted_id}")


@router.post("/{wanted_id}/skip")
def skip(request: Request, wanted_id: int):
    """Give up on the uploader it is downloading from, and try another copy."""
    acq = _engine(request)
    w = _wanted_or_404(request, wanted_id)
    if w["status"] != "downloading":
        if is_fragment(request):
            return toast("It isn't downloading any more", kind="destructive")
        return redirect(f"/wanted/{wanted_id}")
    acq.skip(w)
    acq.wake()
    if is_fragment(request):
        return toast(f"Getting {w['album']} from someone else")
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
    state = request.app.state.state
    if w["status"] not in acquire.CANCELLABLE:
        # Its lookup or import is running, or it is over already.
        if is_fragment(request):
            return toast("It can't be stopped right now", kind="destructive")
        flash(request, f"{w['album']} can't be stopped right now.", "wip")
        return redirect(f"/wanted/{wanted_id}")
    state.update_wanted(wanted_id, status="cancelled", status_text="Cancelled")
    acquire.note(state, w, "You stopped getting it")
    # What it downloaded stays in the inbox, as a folder like any other.
    state.unlink_wanted(wanted_id)
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

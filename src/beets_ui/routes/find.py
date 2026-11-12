"""Find music and the downloads in the inbox, whatever the source.

Pages never name the source: it shows only in a folder's details line and when
it is down. Old /soulseek URLs redirect here.
"""

from __future__ import annotations

import contextlib
import posixpath
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import acquire, discography, rank
from ..slskd import QUEUE_CHOICES, SEARCH_DEFAULTS, SPEED_CHOICES, WAIT_CHOICES
from ..sources import Source, SourceError, coming
from ..web import flash, is_fragment, partial, redirect, render, toast, wanted_cover

router = APIRouter(prefix="/get")
legacy = APIRouter()


def _source(request: Request) -> Source:
    source = request.app.state.source
    if source is None:
        raise HTTPException(
            404, "No download source is set up (for Soulseek: SLSKD_URL and SLSKD_API_KEY)"
        )
    return source


def _refresh_status(request: Request) -> None:
    watch = getattr(request.app.state, "source_watch", None)
    if watch is not None:
        # The page still renders without a fresh status.
        with contextlib.suppress(Exception):
            watch.poll()


def _options(request: Request) -> dict[str, int]:
    """Search options: the last ones used, kept in the session."""
    saved = request.session.get("slsk_opts") or {}
    return {k: int(saved.get(k, v)) for k, v in SEARCH_DEFAULTS.items()}


def _pick(value: str | None, choices: tuple[int, ...], default: int) -> int:
    try:
        v = int(value or "")
    except ValueError:
        return default
    return v if v in choices else default


def _folder_url(search_id: str, origin: str, folder: str) -> str:
    return f"/get/find/s/{search_id}/folder?u={quote(origin, safe='')}&f={quote(folder, safe='')}"


def _results(request: Request, search_id: str, fmt: str = "", free: str = ""):
    source = _source(request)
    info = source.search_info(search_id)
    offers = source.offers(search_id)
    if fmt == "lossless":
        offers = [g for g in offers if g["lossless"]]
    elif fmt:
        offers = [g for g in offers if fmt.upper() in g["format"].split("/")]
    if free:
        offers = [g for g in offers if g["free_slot"]]
    offers = rank.for_search(offers, info.get("text") or "")[:200]
    inbox = request.app.state.settings.inbox
    for g in offers:
        g["in_inbox"] = bool(g["folder_name"]) and (inbox / g["folder_name"]).exists()
        g["url"] = _folder_url(search_id, g["origin"], g["folder"])
    if offers:
        offers[0]["strengths"] = rank.strengths(offers[0])
    return info, offers


def _offer(source: Source, search_id: str, origin: str, folder: str) -> dict | None:
    offers = source.offers(search_id, audio_only=False)
    return next((g for g in offers if g["origin"] == origin and g["folder"] == folder), None)


def _parse_files(values: list[str]) -> list[dict]:
    """Checkbox values are "size:filename"."""
    out = []
    for v in values:
        size, _, name = str(v).partition(":")
        if name and size.isdigit():
            out.append({"filename": name, "size": int(size)})
    return out


# ---------------------------------------------------------------- releases


def _release(request: Request, artist: str, album: str, w: int | None) -> dict | None:
    """The release a search by hand is for ("Find by hand" on Missing, Discover or a
    request), so its searches group under it and a download goes to its request."""
    req = request.app.state.state.wanted(w) if w else None
    if req:
        return {"artist": req["artist"], "album": req["album"], "w": req}
    if album.strip():
        return {"artist": artist.strip(), "album": album.strip(), "w": None}
    return None


def group_searches(state, searches: list[dict]) -> list[dict]:
    """Searches grouped by the release they were for, newest first.

    A search made for a request (by the engine, or by hand from it) groups under the
    request; one made by hand for a release under its artist and album; any other
    search under its own text.
    """
    logged = state.searches_logged([s["id"] for s in searches])
    requests: dict[int, dict | None] = {}
    groups: dict[str, dict] = {}
    for s in searches:
        e = logged.get(s["id"]) or {}
        wid = e.get("wanted_id")
        if wid and wid not in requests:
            requests[wid] = state.wanted(wid)
        w = requests.get(wid) if wid else None
        if w:
            key, title, artist = f"w{wid}", w["album"], w["artist"]
        elif e.get("album"):
            title, artist = e["album"], e.get("artist") or ""
            key = f"r:{discography.norm(artist)}|{discography.norm(title)}"
        else:
            title, artist = s["text"], ""
            key = f"t:{discography.norm(s['text']) or s['id']}"
        g = groups.setdefault(
            key,
            {"key": key, "title": title, "artist": artist, "w": w, "searches": []},
        )
        g["searches"].append(s)
    out = list(groups.values())
    for g in out:
        g["running"] = any(s["running"] for s in g["searches"])
        g["answers"] = sum(s["answers"] or 0 for s in g["searches"])
        g["latest"] = g["searches"][0]
        g["cover"] = wanted_cover(g["w"]) if g["w"] else None
        # For a release (a request, or one named when searching by hand), or for none.
        g["loose"] = g["key"].startswith("t:")
    return out


# Which searches Find lists: those made for a release (the default), or the others.
SHOWS = ("release", "other")


# -------------------------------------------------------------------- find


@router.get("/find")
def find_page(
    request: Request,
    q: str = "",
    artist: str = "",
    album: str = "",
    w: int | None = None,
    show: str = "release",
):
    source = _source(request)
    error = None
    try:
        searches = source.searches()[:60]
    except SourceError as e:
        searches, error = [], str(e)
    show = show if show in SHOWS else "release"
    groups = group_searches(request.app.state.state, searches)
    counts = {
        "release": sum(1 for g in groups if not g["loose"]),
        "other": sum(1 for g in groups if g["loose"]),
    }
    return render(
        request,
        "find/index.html",
        nav="get",
        q=q,
        release=_release(request, artist, album, w),
        groups=[g for g in groups if g["loose"] == (show == "other")],
        counts=counts,
        show=show,
        error=error,
        opts=_options(request),
        wait_choices=WAIT_CHOICES,
        queue_choices=QUEUE_CHOICES,
        speed_choices=SPEED_CHOICES,
    )


@router.post("/find/search")
def search(
    request: Request,
    q: str = Form(""),
    wait: str | None = Form(None),
    max_queue: str | None = Form(None),
    min_speed: str | None = Form(None),
    artist: str = Form(""),
    album: str = Form(""),
    w: int | None = Form(None),
):
    q = q.strip()
    opts = _options(request)
    if wait is not None:
        opts = {
            "wait": _pick(wait, WAIT_CHOICES, SEARCH_DEFAULTS["wait"]),
            "max_queue": _pick(max_queue, QUEUE_CHOICES, 0),
            "min_speed": _pick(min_speed, SPEED_CHOICES, 0),
        }
        request.session["slsk_opts"] = opts
    if len(q) < 3:
        flash(request, "Search text must be at least 3 characters.", "wip")
        return redirect("/get/find")
    try:
        sid = _source(request).search(q, **opts)
    except SourceError as e:
        flash(request, str(e), "warn")
        return redirect("/get/find")
    release = _release(request, artist, album, w)
    if release:
        wid = release["w"]["id"] if release["w"] else None
        request.app.state.state.log_search(sid, q, release["artist"], release["album"], wid)
    return redirect(f"/get/find/s/{sid}")


def _search_release(request: Request, search_id: str) -> dict | None:
    """The release a search was for, if it was made for one."""
    e = request.app.state.state.searches_logged([search_id]).get(search_id)
    if not e:
        return None
    return _release(request, e["artist"], e["album"], e["wanted_id"])


@router.get("/find/s/{search_id}")
def search_page(request: Request, search_id: str, fmt: str = "", free: str = ""):
    try:
        info, offers = _results(request, search_id, fmt, free)
    except SourceError as e:
        flash(request, str(e), "warn")
        return redirect("/get/find")
    return render(
        request,
        "find/search.html",
        nav="get",
        release=_search_release(request, search_id),
        search=info,
        groups=offers,
        search_id=search_id,
        fmt=fmt,
        free=free,
    )


@router.get("/find/s/{search_id}/results")
def search_results(request: Request, search_id: str, fmt: str = "", free: str = ""):
    info, offers = _results(request, search_id, fmt, free)
    return partial(
        request,
        "find/_results.html",
        search=info,
        groups=offers,
        search_id=search_id,
        fmt=fmt,
        free=free,
    )


@router.post("/find/s/{search_id}/stop")
def search_stop(request: Request, search_id: str):
    try:
        _source(request).stop_search(search_id)
    except SourceError as e:
        flash(request, str(e), "warn")
    return redirect(f"/get/find/s/{search_id}")


@router.post("/find/s/{search_id}/delete")
def search_delete(request: Request, search_id: str):
    try:
        _source(request).delete_search(search_id)
    except SourceError as e:
        if is_fragment(request):
            resp = toast("Couldn't delete the search", str(e), "destructive")
            resp.status_code = 409
            return resp
        flash(request, str(e), "warn")
    if is_fragment(request):
        return toast("Search deleted")
    return redirect("/get/find")


@router.get("/find/s/{search_id}/folder")
def folder_page(request: Request, search_id: str, u: str = "", f: str = ""):
    source = _source(request)
    try:
        offer = _offer(source, search_id, u, f)
        info = source.search_info(search_id)
    except SourceError as e:
        flash(request, str(e), "warn")
        return redirect(f"/get/find/s/{search_id}")
    if offer is None:
        flash(request, "That folder is no longer in the search results.", "warn")
        return redirect(f"/get/find/s/{search_id}")
    inbox = request.app.state.settings.inbox
    offer["in_inbox"] = bool(offer["folder_name"]) and (inbox / offer["folder_name"]).exists()
    return render(
        request,
        "find/folder.html",
        nav="get",
        g=offer,
        search=info,
        search_id=search_id,
        source_label=source.label,
        files_url=_folder_url(search_id, u, f).replace("/folder?", "/folder/files?", 1),
    )


@router.get("/find/s/{search_id}/folder/files")
def folder_files(request: Request, search_id: str, u: str = "", f: str = ""):
    """The folder's full listing, asked of its origin; the matched files if it doesn't answer."""
    source = _source(request)
    offer = _offer(source, search_id, u, f)
    if offer is None:
        return partial(request, "find/_folder_files.html", files=[], gone=True)
    files, problem = [], ""
    try:
        files = source.listing(u, offer["path"])
    except SourceError as e:
        problem = str(e)
    if not files:
        files = offer["files"]
        problem = problem or f"{u} sent an empty listing."
    return partial(
        request,
        "find/_folder_files.html",
        g=offer,
        files=files,
        problem=problem,
        search_id=search_id,
        extra=len(files) - len(offer["files"]) if not problem else 0,
    )


@router.post("/find/download")
def download(
    request: Request,
    search_id: str = Form(...),
    username: str = Form(...),
    folder: str = Form(...),
    file: list[str] = Form([]),
):
    """Download a folder: the files ticked on its page, or the whole folder from the results."""
    source = _source(request)
    back = f"/get/find/s/{search_id}"
    note = ""
    try:
        if file:
            files = _parse_files(file)
            name = posixpath.basename(folder) or folder
            if not files:
                flash(request, "Choose at least one file.", "wip")
                return redirect(_folder_url(search_id, username, folder))
        else:
            offer = _offer(source, search_id, username, folder)
            if offer is None:
                flash(request, "That folder is no longer in the search results.", "warn")
                return redirect(back)
            name = offer["folder_name"]
            try:
                files = source.listing(username, offer["path"])
            except SourceError:
                files = []
            if not files:
                files = offer["files"]
                note = (
                    f" {username} didn't send the full folder, so only the files that matched"
                    " the search were queued."
                )
        source.fetch(username, files)
    except SourceError as e:
        flash(request, str(e), "warn")
        return redirect(back)
    try:
        text = source.search_info(search_id).get("text") or ""
    except SourceError:
        text = ""
    release = _search_release(request, search_id)
    acq = getattr(request.app.state, "acquirer", None)
    n = len(files)
    what = f"{n} file{'s' if n != 1 else ''} from {username}: {name}"
    if release and release["w"] and acq and acq.adopt(release["w"], username, folder, name):
        # The request follows this download and imports it against its release.
        flash(
            request,
            f"Downloading {what}, for {release['album']}. Follow it under Coming in.{note}",
            "wip" if note else "ok",
        )
        return redirect(back)
    acquire.remember_fetch(
        request.app.state.state,
        origin=username,
        folder=folder,
        folder_name=name,
        search_id=search_id,
        text=text,
        tracks=rank.audio_count(files),
        artist=release["artist"] if release else "",
        album=release["album"] if release else "",
    )
    flash(request, f"Downloading {what}. Follow it under Coming in.{note}", "wip" if note else "ok")
    return redirect(back)


# --------------------------------------------------------------- downloads


def _downloads_done(request: Request, message, ok: bool = True):
    """In place: the downloads list again, fresh, with a pop-up. Otherwise back to Coming in."""
    if not is_fragment(request):
        if message:
            flash(request, message[0], "ok" if ok else "warn")
        return redirect("/get/coming")
    _refresh_status(request)
    resp = partial(request, "get/_coming.html", items=coming(request.app))
    if message:
        title, text = message
        resp.headers["HX-Trigger"] = toast(title, text, "success" if ok else "destructive").headers[
            "HX-Trigger"
        ]
    return resp


@router.get("/downloads")
def downloads(request: Request):
    return partial(request, "get/_coming.html", items=coming(request.app))


@router.post("/downloads/cancel")
def downloads_cancel(
    request: Request,
    username: str = Form(...),
    id: list[str] = Form([]),
    remove: str = Form(""),
    name: str = Form(""),
):
    try:
        _source(request).cancel(username, id, remove=bool(remove))
    except SourceError as e:
        return _downloads_done(request, (str(e), ""), ok=False)
    what = "Removed" if remove else "Cancelled"
    return _downloads_done(request, (f"{what} {name}" if name else what, ""))


@router.post("/downloads/retry")
def downloads_retry(
    request: Request,
    username: str = Form(...),
    file: list[str] = Form([]),
    id: list[str] = Form([]),
    name: str = Form(""),
):
    """Download failed files again, then drop their failed entries from the list."""
    source = _source(request)
    files = _parse_files(file)
    if not files:
        return _downloads_done(request, None)
    try:
        source.fetch(username, files)
    except SourceError as e:
        return _downloads_done(request, (str(e), ""), ok=False)
    # If removing fails the old entry stays listed; the retry is queued all the same.
    with contextlib.suppress(SourceError):
        source.cancel(username, id, remove=True)
    n = len(files)
    title = f"Retrying {name}" if name else "Retrying"
    return _downloads_done(request, (title, f"{n} file{'s' if n != 1 else ''} from {username}."))


@router.post("/downloads/another")
def downloads_another(
    request: Request, username: str = Form(...), folder: str = Form(...), name: str = Form("")
):
    """Stop waiting on this uploader: the same folder from the next one who has it.

    The download engine does the looking (searching again if nobody in the first
    search has it), so this answers at once; the current download keeps going until then.
    """
    _source(request)
    acq = getattr(request.app.state, "acquirer", None)
    name = name or posixpath.basename(folder.replace("\\", "/")) or folder
    if acq is None:
        return _downloads_done(request, (f"Can't get {name} from someone else", ""), ok=False)
    acq.another(username, folder, name)
    return _downloads_done(
        request,
        (
            f"Getting {name} from someone else",
            f"It switches to the next uploader with this folder. Until then {username} keeps going.",
        ),
    )


@router.post("/downloads/clear")
def downloads_clear(request: Request):
    try:
        _source(request).clear_finished()
    except SourceError as e:
        return _downloads_done(request, (str(e), ""), ok=False)
    return _downloads_done(request, ("Cleared finished downloads", ""))


@router.post("/sources/connect")
def source_connect(request: Request, back: str = Form("/get/coming")):
    try:
        _source(request).connect()
        flash(request, "Reconnecting the download source.", "ok")
    except SourceError as e:
        flash(request, str(e), "warn")
    _refresh_status(request)
    return redirect(back if back.startswith("/") and not back.startswith("//") else "/get/coming")


# ------------------------------------------------------------- old addresses


@legacy.get("/soulseek{rest:path}", include_in_schema=False)
def old_soulseek(request: Request, rest: str = ""):
    """Links from before Soulseek moved into the inbox keep working."""
    if rest.startswith("/transfers"):
        target = "/get/coming"
    else:
        target = "/get/find" + rest
        if request.url.query:
            target += "?" + request.url.query
    return RedirectResponse(target, status_code=307)

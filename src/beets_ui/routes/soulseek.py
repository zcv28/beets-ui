from __future__ import annotations

import contextlib
import posixpath
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request

from ..slskd import (
    QUEUE_CHOICES,
    SEARCH_DEFAULTS,
    SPEED_CHOICES,
    WAIT_CHOICES,
    Slskd,
    SlskdError,
    flatten_downloads,
    group_responses,
)
from ..web import flash, partial, redirect, render

router = APIRouter(prefix="/soulseek")


def _client(request: Request) -> Slskd:
    client = request.app.state.slskd
    if client is None:
        raise HTTPException(
            404, "slskd integration is not configured (set SLSKD_URL and SLSKD_API_KEY)"
        )
    return client


def _refresh_status(request: Request) -> None:
    watch = getattr(request.app.state, "slskd_watch", None)
    if watch is not None:
        # The page still renders without a fresh status.
        with contextlib.suppress(Exception):
            watch.poll()


def _options(request: Request) -> dict[str, int]:
    """Search options: the last ones used, kept in the session."""
    saved = request.session.get("slsk_opts") or {}
    return {k: int(saved.get(k, v)) for k, v in SEARCH_DEFAULTS.items()}


def _pick(value: str, choices: tuple[int, ...], default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return v if v in choices else default


def search_label(state: str) -> tuple[str, str]:
    """(status token, label) for a slskd search state."""
    s = str(state or "")
    if "Completed" not in s:
        return "running", "Searching"
    if "Cancelled" in s:
        return "", "Stopped"
    return "", "Finished"


def _folder_url(search_id: str, username: str, folder: str) -> str:
    return f"/soulseek/s/{search_id}/folder?u={quote(username, safe='')}&f={quote(folder, safe='')}"


def _results(request: Request, search_id: str, fmt: str = "", free: str = ""):
    client = _client(request)
    state = client.search_state(search_id) or {}
    groups = group_responses(client.search_responses(search_id))
    if fmt == "lossless":
        groups = [g for g in groups if g["lossless"]]
    elif fmt:
        groups = [g for g in groups if fmt.upper() in g["format"].split("/")]
    if free:
        groups = [g for g in groups if g["free_slot"]]
    inbox = request.app.state.settings.inbox
    for g in groups:
        g["in_inbox"] = bool(g["folder_name"]) and (inbox / g["folder_name"]).exists()
        g["url"] = _folder_url(search_id, g["username"], g["folder"])
    ui_state, label = search_label(state.get("state", ""))
    return state, groups[:200], ui_state != "running", label


def _group(client: Slskd, search_id: str, username: str, folder: str) -> dict | None:
    groups = group_responses(client.search_responses(search_id), audio_only=False)
    return next((g for g in groups if g["username"] == username and g["folder"] == folder), None)


def _parse_files(values: list[str]) -> list[dict]:
    """Checkbox values are "size:filename"."""
    out = []
    for v in values:
        size, _, name = str(v).partition(":")
        if name and size.isdigit():
            out.append({"filename": name, "size": int(size)})
    return out


# ------------------------------------------------------------------ search


@router.get("")
def soulseek_page(request: Request, q: str = ""):
    client = _client(request)
    error = None
    try:
        searches = sorted(client.searches(), key=lambda s: s.get("startedAt", ""), reverse=True)[
            :15
        ]
    except SlskdError as e:
        searches, error = [], str(e)
    for s in searches:
        s["ui_state"], s["ui_label"] = search_label(s.get("state", ""))
    return render(
        request,
        "soulseek/index.html",
        nav="soulseek",
        q=q,
        searches=searches,
        error=error,
        opts=_options(request),
        wait_choices=WAIT_CHOICES,
        queue_choices=QUEUE_CHOICES,
        speed_choices=SPEED_CHOICES,
    )


@router.post("/search")
def search(
    request: Request,
    q: str = Form(""),
    wait: str | None = Form(None),
    max_queue: str | None = Form(None),
    min_speed: str | None = Form(None),
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
        return redirect("/soulseek")
    try:
        sid = _client(request).search(q, **opts)
    except SlskdError as e:
        flash(request, str(e), "warn")
        return redirect("/soulseek")
    return redirect(f"/soulseek/s/{sid}")


@router.get("/s/{search_id}")
def search_page(request: Request, search_id: str, fmt: str = "", free: str = ""):
    try:
        state, groups, done, label = _results(request, search_id, fmt, free)
    except SlskdError as e:
        flash(request, str(e), "warn")
        return redirect("/soulseek")
    return render(
        request,
        "soulseek/search.html",
        nav="soulseek",
        search=state,
        groups=groups,
        done=done,
        label=label,
        search_id=search_id,
        fmt=fmt,
        free=free,
    )


@router.get("/s/{search_id}/results")
def search_results(request: Request, search_id: str, fmt: str = "", free: str = ""):
    state, groups, done, label = _results(request, search_id, fmt, free)
    return partial(
        request,
        "soulseek/_results.html",
        search=state,
        groups=groups,
        done=done,
        label=label,
        search_id=search_id,
        fmt=fmt,
        free=free,
    )


@router.post("/s/{search_id}/stop")
def search_stop(request: Request, search_id: str):
    try:
        _client(request).stop_search(search_id)
    except SlskdError as e:
        flash(request, str(e), "warn")
    return redirect(f"/soulseek/s/{search_id}")


@router.post("/s/{search_id}/delete")
def search_delete(request: Request, search_id: str):
    try:
        _client(request).delete_search(search_id)
    except SlskdError as e:
        flash(request, str(e), "warn")
    return redirect("/soulseek")


# ------------------------------------------------------------------ folder


@router.get("/s/{search_id}/folder")
def folder_page(request: Request, search_id: str, u: str = "", f: str = ""):
    client = _client(request)
    try:
        group = _group(client, search_id, u, f)
        state = client.search_state(search_id) or {}
    except SlskdError as e:
        flash(request, str(e), "warn")
        return redirect(f"/soulseek/s/{search_id}")
    if group is None:
        flash(request, "That folder is no longer in the search results.", "warn")
        return redirect(f"/soulseek/s/{search_id}")
    inbox = request.app.state.settings.inbox
    group["in_inbox"] = bool(group["folder_name"]) and (inbox / group["folder_name"]).exists()
    return render(
        request,
        "soulseek/folder.html",
        nav="soulseek",
        g=group,
        search=state,
        search_id=search_id,
        files_url=_folder_url(search_id, u, f).replace("/folder?", "/folder/files?", 1),
    )


@router.get("/s/{search_id}/folder/files")
def folder_files(request: Request, search_id: str, u: str = "", f: str = ""):
    """The folder's full listing, asked of the user; the matched files if they don't answer."""
    client = _client(request)
    group = _group(client, search_id, u, f)
    if group is None:
        return partial(request, "soulseek/_folder_files.html", files=[], gone=True)
    files, problem = [], ""
    try:
        files = client.directory(u, group["path"])
    except SlskdError as e:
        problem = str(e)
    if not files:
        files = group["files"]
        problem = problem or f"{u} sent an empty listing."
    return partial(
        request,
        "soulseek/_folder_files.html",
        g=group,
        files=files,
        problem=problem,
        search_id=search_id,
        extra=len(files) - len(group["files"]) if not problem else 0,
    )


@router.post("/download")
def download(
    request: Request,
    search_id: str = Form(...),
    username: str = Form(...),
    folder: str = Form(...),
    file: list[str] = Form([]),
):
    """Queue a folder: the files ticked on its page, or the whole folder from the results."""
    client = _client(request)
    back = f"/soulseek/s/{search_id}"
    note = ""
    try:
        if file:
            files = _parse_files(file)
            name = posixpath.basename(folder) or folder
            if not files:
                flash(request, "Choose at least one file.", "wip")
                return redirect(_folder_url(search_id, username, folder))
        else:
            group = _group(client, search_id, username, folder)
            if group is None:
                flash(request, "That folder is no longer in the search results.", "warn")
                return redirect(back)
            name = group["folder_name"]
            try:
                files = client.directory(username, group["path"])
            except SlskdError:
                files = []
            if not files:
                files = group["files"]
                note = (
                    f" {username} didn't send the full folder, so only the files that matched"
                    " the search were queued."
                )
        client.enqueue(username, files)
    except SlskdError as e:
        flash(request, str(e), "warn")
        return redirect(back)
    n = len(files)
    flash(
        request,
        f"Queued {n} file{'s' if n != 1 else ''} from {username}: {name}. "
        f"Finished downloads appear in the inbox.{note}",
        "wip" if note else "ok",
    )
    return redirect(back)


# --------------------------------------------------------------- transfers


def _transfers(request: Request):
    try:
        return flatten_downloads(_client(request).downloads()), None
    except SlskdError as e:
        return [], str(e)


@router.get("/transfers")
def transfers(request: Request):
    rows, error = _transfers(request)
    return render(request, "soulseek/transfers.html", nav="soulseek", rows=rows, error=error)


@router.get("/transfers/table")
def transfers_table(request: Request):
    rows, error = _transfers(request)
    return partial(request, "soulseek/_transfers.html", rows=rows, error=error)


@router.post("/transfers/cancel")
def transfers_cancel(
    request: Request,
    username: str = Form(...),
    id: list[str] = Form([]),
    remove: str = Form(""),
):
    client = _client(request)
    try:
        for tid in id:
            client.cancel(username, tid, remove=bool(remove))
    except SlskdError as e:
        flash(request, str(e), "warn")
    return redirect("/soulseek/transfers")


@router.post("/transfers/retry")
def transfers_retry(
    request: Request,
    username: str = Form(...),
    file: list[str] = Form([]),
    id: list[str] = Form([]),
):
    """Queue failed files again, then drop their failed entries from the list."""
    client = _client(request)
    files = _parse_files(file)
    if not files:
        return redirect("/soulseek/transfers")
    try:
        client.enqueue(username, files)
    except SlskdError as e:
        flash(request, str(e), "warn")
        return redirect("/soulseek/transfers")
    # If removing fails the old entry stays listed; the retry is queued all the same.
    for tid in id:
        with contextlib.suppress(SlskdError):
            client.cancel(username, tid, remove=True)
    n = len(files)
    flash(request, f"Queued {n} file{'s' if n != 1 else ''} from {username} again.", "ok")
    return redirect("/soulseek/transfers")


@router.post("/transfers/clear")
def transfers_clear(request: Request):
    try:
        _client(request).clear_completed()
        flash(request, "Cleared finished and failed downloads from the list.")
    except SlskdError as e:
        flash(request, str(e), "warn")
    return redirect("/soulseek/transfers")


# ------------------------------------------------------------------ server


@router.post("/server/connect")
def server_connect(request: Request, back: str = Form("/soulseek")):
    try:
        _client(request).connect()
        flash(request, "Asked slskd to connect to Soulseek.", "ok")
    except SlskdError as e:
        flash(request, str(e), "warn")
    _refresh_status(request)
    return redirect(back if back.startswith("/soulseek") else "/soulseek")

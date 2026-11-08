from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request

from ..slskd import Slskd, SlskdError, flatten_downloads, group_responses
from ..web import flash, partial, redirect, render

router = APIRouter(prefix="/soulseek")


def _client(request: Request) -> Slskd:
    client = request.app.state.slskd
    if client is None:
        raise HTTPException(
            404, "slskd integration is not configured (set SLSKD_URL and SLSKD_API_KEY)"
        )
    return client


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
    done = "Completed" in str(state.get("state", ""))
    return state, groups[:200], done


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
    return render(
        request, "soulseek/index.html", nav="soulseek", q=q, searches=searches, error=error
    )


@router.post("/search")
def search(request: Request, q: str = Form("")):
    q = q.strip()
    if len(q) < 3:
        flash(request, "Search text must be at least 3 characters.", "wip")
        return redirect("/soulseek")
    try:
        sid = _client(request).search(q)
    except SlskdError as e:
        flash(request, str(e), "warn")
        return redirect("/soulseek")
    return redirect(f"/soulseek/s/{sid}")


@router.get("/s/{search_id}")
def search_page(request: Request, search_id: str, fmt: str = "", free: str = ""):
    try:
        state, groups, done = _results(request, search_id, fmt, free)
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
        search_id=search_id,
        fmt=fmt,
        free=free,
    )


@router.get("/s/{search_id}/results")
def search_results(request: Request, search_id: str, fmt: str = "", free: str = ""):
    state, groups, done = _results(request, search_id, fmt, free)
    return partial(
        request,
        "soulseek/_results.html",
        search=state,
        groups=groups,
        done=done,
        search_id=search_id,
        fmt=fmt,
        free=free,
    )


@router.post("/s/{search_id}/delete")
def search_delete(request: Request, search_id: str):
    try:
        _client(request).delete_search(search_id)
    except SlskdError as e:
        flash(request, str(e), "warn")
    return redirect("/soulseek")


@router.post("/download")
def download(
    request: Request,
    search_id: str = Form(...),
    username: str = Form(...),
    folder: str = Form(...),
):
    client = _client(request)
    try:
        groups = group_responses(client.search_responses(search_id), audio_only=False)
        match = next(
            (g for g in groups if g["username"] == username and g["folder"] == folder), None
        )
        if match is None:
            flash(request, "That folder is no longer in the search results.", "warn")
            return redirect(f"/soulseek/s/{search_id}")
        client.enqueue(username, match["files"])
    except SlskdError as e:
        flash(request, str(e), "warn")
        return redirect(f"/soulseek/s/{search_id}")
    flash(
        request,
        f"Queued {len(match['files'])} files from {username}: {match['folder_name']}. "
        "Finished downloads appear in the inbox automatically.",
    )
    return redirect(f"/soulseek/s/{search_id}")


@router.get("/transfers")
def transfers(request: Request):
    error = None
    try:
        rows = flatten_downloads(_client(request).downloads())
    except SlskdError as e:
        rows, error = [], str(e)
    return render(request, "soulseek/transfers.html", nav="soulseek", rows=rows, error=error)


@router.get("/transfers/table")
def transfers_table(request: Request):
    try:
        rows = flatten_downloads(_client(request).downloads())
        error = None
    except SlskdError as e:
        rows, error = [], str(e)
    return partial(request, "soulseek/_transfers.html", rows=rows, error=error)


@router.post("/transfers/cancel")
async def transfers_cancel(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))
    ids = [str(i) for i in form.getlist("id")]
    client = _client(request)
    try:
        for tid in ids:
            client.cancel(username, tid, remove=bool(form.get("remove")))
    except SlskdError as e:
        flash(request, str(e), "warn")
    return redirect("/soulseek/transfers")


@router.post("/transfers/clear")
def transfers_clear(request: Request):
    try:
        _client(request).clear_completed()
        flash(request, "Cleared completed transfers.")
    except SlskdError as e:
        flash(request, str(e), "warn")
    return redirect("/soulseek/transfers")

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request

from ..importer import DUPLICATE_ACTIONS
from ..inbox import STATUS_LABELS
from ..web import flash, partial, redirect, render, user

router = APIRouter(prefix="/inbox")

BUSY = ("queued", "previewing", "importing")


def _furl(f: str) -> str:
    return "/inbox/folder?f=" + quote(f, safe="")


def _folder_or_404(request: Request, f: str) -> dict:
    row = request.app.state.state.folder(f)
    if row is None:
        raise HTTPException(404, "folder not found (it may have been removed from the inbox)")
    return row


def _summary(rows: list[dict]) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        out[r["status"]] = out.get(r["status"], 0) + 1
    return out


@router.get("")
def inbox_page(request: Request, status: str = ""):
    rows = request.app.state.state.folders()
    shown = [r for r in rows if not status or r["status"] == status]
    return render(
        request,
        "inbox/index.html",
        nav="inbox",
        folders=shown,
        summary=_summary(rows),
        labels=STATUS_LABELS,
        status=status,
        settings=request.app.state.settings,
    )


@router.get("/table")
def inbox_table(request: Request, status: str = ""):
    rows = request.app.state.state.folders()
    shown = [r for r in rows if not status or r["status"] == status]
    return partial(request, "inbox/_table.html", folders=shown, labels=STATUS_LABELS)


@router.post("/scan")
def scan(request: Request):
    request.app.state.inbox.scan()
    flash(request, "Inbox rescanned.")
    return redirect("/inbox")


@router.post("/preview-all")
def preview_all(request: Request):
    n = 0
    for row in request.app.state.state.folders():
        if row["status"] in ("new", "failed"):
            request.app.state.inbox.queue_preview(row["path"], user=user(request))
            n += 1
    flash(request, f"Queued lookups for {n} folder(s).")
    return redirect("/inbox")


@router.get("/folder")
def folder_page(request: Request, f: str):
    row = _folder_or_404(request, f)
    s = request.app.state.settings
    return render(
        request,
        "inbox/folder.html",
        nav="inbox",
        folder=row,
        labels=STATUS_LABELS,
        busy=row["status"] in BUSY,
        dup_actions=list(DUPLICATE_ACTIONS),
        inbox_root=str(s.inbox.resolve()),
    )


@router.get("/folder/status")
def folder_status(request: Request, f: str, was: str = ""):
    row = request.app.state.state.folder(f)
    if row is None or (was in BUSY and row["status"] not in BUSY):
        # Work finished: reload the whole page to show the new results.
        resp = partial(request, "partials/empty.html")
        resp.headers["HX-Refresh"] = "true"
        return resp
    return partial(request, "inbox/_busy.html", folder=row, labels=STATUS_LABELS)


@router.post("/folder/preview")
def folder_preview(
    request: Request,
    f: str = Form(...),
    mode: str = Form(""),
    artist: str = Form(""),
    album: str = Form(""),
    ids: str = Form(""),
    merge: str = Form(""),
):
    row = _folder_or_404(request, f)
    if row["status"] in BUSY:
        flash(request, "This folder is already being processed.", "wip")
        return redirect(_furl(f))
    mode = mode if mode in ("albums", "grouped", "tracks") else row["mode"]
    id_list = [i for i in re.split(r"[\s,]+", ids.strip()) if i]
    request.app.state.inbox.queue_preview(
        f,
        mode=mode,
        user=user(request),
        artist=artist.strip() or None,
        album=album.strip() or None,
        ids=id_list or None,
        merge=bool(merge) and mode == row["mode"],
    )
    return redirect(_furl(f))


@router.post("/folder/import")
async def folder_import(request: Request):
    form = await request.form()
    f = str(form.get("f", ""))
    row = _folder_or_404(request, f)
    if row["status"] in BUSY:
        flash(request, "This folder is already being processed.", "wip")
        return redirect(_furl(f))
    decisions = {}
    for i, task in enumerate(row["tasks"]):
        choice = str(form.get(f"choice-{i}", "skip"))
        dup = str(form.get(f"dup-{i}", "")) or None
        if choice.startswith("c:"):
            try:
                cand = task["candidates"][int(choice[2:])]
            except (ValueError, IndexError):
                raise HTTPException(400, "invalid candidate") from None
            dec = {"action": "apply", "id": cand["id"], "source": cand["source"]}
        elif choice in ("asis", "skip"):
            dec = {"action": choice}
        else:
            raise HTTPException(400, "invalid choice")
        if dup in DUPLICATE_ACTIONS:
            dec["duplicate"] = dup
        decisions[task["key"]] = dec
    if all(d["action"] == "skip" for d in decisions.values()):
        flash(request, "Every album is set to skip; nothing to import.", "wip")
        return redirect(_furl(f))
    request.app.state.inbox.queue_import(f, decisions, user=user(request))
    return redirect(_furl(f))


@router.post("/folder/delete")
def folder_delete(request: Request, f: str = Form(...)):
    row = _folder_or_404(request, f)
    if row["status"] in BUSY:
        flash(request, "Cannot delete a folder that is being processed.", "warn")
        return redirect(_furl(f))
    try:
        request.app.state.inbox.delete_source(f)
    except (OSError, ValueError) as e:
        flash(request, f"Could not delete: {e}", "warn")
        return redirect(_furl(f))
    flash(request, f"Deleted {f} from the inbox.")
    return redirect("/inbox")

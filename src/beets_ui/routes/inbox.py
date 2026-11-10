from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, Response

from ..importer import DUPLICATE_ACTIONS
from ..inbox import AUDIO_EXT, STATUS_LABELS, is_within
from ..web import flash, partial, redirect, render, user

router = APIRouter(prefix="/inbox")

BUSY = ("queued", "previewing", "importing")
# Folders waiting for a decision: the review queue, in inbox order.
REVIEW = ("ready", "attention", "duplicate")


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


def _deletable(row: dict) -> bool:
    return row["status"] not in BUSY and (row["status"] == "imported" or bool(row["library"]))


def _queue(rows: list[dict], current: str) -> dict:
    """Where `current` sits in the review queue, and the folders before and after it."""
    order = {r["path"]: i for i, r in enumerate(rows)}
    waiting = [r["path"] for r in rows if r["status"] in REVIEW]
    others = [p for p in waiting if p != current]
    here = order.get(current, -1)
    after = [p for p in others if order[p] > here]
    before = [p for p in others if order[p] < here]
    return {
        "pos": waiting.index(current) + 1 if current in waiting else 0,
        "total": len(waiting),
        "left": len(others),
        "next": (after or before or [None])[0],
        "prev": (before or after or [None])[-1],
    }


def _version(rows: list[dict]) -> str:
    """Changes whenever anything the inbox table shows changes."""
    blob = json.dumps(rows, sort_keys=True, default=str).encode()
    return hashlib.md5(blob).hexdigest()[:12]


def _filter(rows: list[dict], status: str) -> list[dict]:
    if status == "inlib":
        return [r for r in rows if _deletable(r)]
    return [r for r in rows if not status or r["status"] == status]


@router.get("")
def inbox_page(request: Request, status: str = ""):
    rows = request.app.state.state.folders()
    return render(
        request,
        "inbox/index.html",
        nav="inbox",
        folders=_filter(rows, status),
        summary=_summary(rows),
        imported=sum(1 for r in rows if _deletable(r)),
        to_review=_queue(rows, ""),
        labels=STATUS_LABELS,
        status=status,
        version=_version(rows),
        settings=request.app.state.settings,
    )


@router.get("/table")
def inbox_table(request: Request, status: str = "", v: str = ""):
    rows = request.app.state.state.folders()
    version = _version(rows)
    if v == version:
        # Nothing changed: htmx leaves the table (and the focus in it) alone.
        return Response(status_code=204)
    return partial(
        request,
        "inbox/_table.html",
        folders=_filter(rows, status),
        labels=STATUS_LABELS,
        status=status,
        version=version,
    )


@router.post("/delete-imported")
def delete_imported(request: Request):
    deleted = request.app.state.inbox.delete_imported()
    flash(request, f"Deleted {len(deleted)} imported folder(s) from the inbox.")
    return redirect("/inbox")


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
            request.app.state.inbox.queue_preview(row["path"], user=user(request), auto=True)
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
        queue=_queue(request.app.state.state.folders(), f),
        labels=STATUS_LABELS,
        busy=row["status"] in BUSY,
        dup_actions=list(DUPLICATE_ACTIONS),
        inbox_root=str(s.inbox.resolve()),
        romanize=s.romanize,
    )


# Browsers guess poorly for some audio types; FLAC and Opus play in every current browser.
AUDIO_TYPES = {".flac": "audio/flac", ".opus": "audio/ogg", ".m4a": "audio/mp4"}


@router.get("/file")
def inbox_file(request: Request, p: str):
    """Stream one audio file from the inbox for the review page's player."""
    root = request.app.state.settings.inbox
    path = Path(p) if Path(p).is_absolute() else root / p
    if not is_within(path, root) or path.suffix.lower() not in AUDIO_EXT or not path.is_file():
        raise HTTPException(404, "no such audio file in the inbox")
    media = AUDIO_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
    return FileResponse(path, media_type=media or "application/octet-stream")


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
    job = request.app.state.inbox.queue_import(f, decisions, user=user(request))
    if not form.get("next"):
        return redirect(_furl(f))
    # Keep the session moving: the import runs as a job while the next folder is up.
    done = int(request.session.get("review_run", 0)) + 1
    nxt = _queue(request.app.state.state.folders(), f)["next"]
    if nxt:
        request.session["review_run"] = done
        flash(request, f"Importing {f} as job #{job}. A pop-up reports when it is done.")
        return redirect(_furl(nxt))
    request.session.pop("review_run", None)
    flash(
        request,
        f"Importing {f} as job #{job}. That was the last one: {done} folder"
        f"{'s' if done != 1 else ''} imported in this session, nothing left to review.",
        "ok",
    )
    return redirect("/inbox")


@router.post("/folder/delete")
def folder_delete(request: Request, f: str = Form(...), back: str = Form("")):
    row = _folder_or_404(request, f)
    to_list = back.startswith("/inbox") and not back.startswith("//")
    if row["status"] in BUSY:
        flash(request, "Cannot delete a folder that is being processed.", "warn")
        return redirect(back if to_list else _furl(f))
    try:
        request.app.state.inbox.delete_source(f)
    except (OSError, ValueError) as e:
        flash(request, f"Could not delete {f}: {e}", "warn")
        return redirect(back if to_list else _furl(f))
    flash(request, f"Deleted {f} from the inbox.")
    return redirect(back if to_list else "/inbox")

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, Response

from .. import beets_ctx
from .. import library as libsvc
from ..web import HERE, flash, redirect, render, user

router = APIRouter(prefix="/library")
NOCOVER = HERE / "static" / "nocover.svg"


def _album_or_404(album_id: int):
    album = beets_ctx.lib().get_album(album_id)
    if album is None:
        raise HTTPException(404, "album not found")
    return album


def _item_or_404(item_id: int):
    item = beets_ctx.lib().get_item(item_id)
    if item is None:
        raise HTTPException(404, "track not found")
    return item


@router.get("")
def albums(request: Request, q: str = "", sort: str = "artist", page: int = 1, view: str = "grid"):
    page = max(1, page)
    error = None
    try:
        rows, more = libsvc.albums(q, sort, page)
    except libsvc.QueryError as e:
        rows, more, error = [], False, str(e)
    return render(
        request,
        "library/albums.html",
        nav="library",
        albums=rows,
        more=more,
        q=q,
        sort=sort,
        page=page,
        view=view,
        error=error,
        stats=libsvc.stats() if not q else None,
        sorts=list(libsvc.ALBUM_SORTS),
    )


@router.get("/tracks")
def tracks(request: Request, q: str = "", page: int = 1, singletons: int = 0):
    page = max(1, page)
    error = None
    try:
        rows, more = libsvc.items(q, page, singletons=bool(singletons))
    except libsvc.QueryError as e:
        rows, more, error = [], False, str(e)
    return render(
        request,
        "library/tracks.html",
        nav="library",
        items=rows,
        more=more,
        q=q,
        page=page,
        singletons=singletons,
        error=error,
    )


@router.get("/artists")
def artists(request: Request):
    return render(
        request,
        "library/artists.html",
        nav="library",
        artists=request.app.state.artwork.artists(),
    )


@router.get("/album/{album_id}")
def album(request: Request, album_id: int):
    a = _album_or_404(album_id)
    items = list(a.items())
    fmt = a.formatted()
    fields = {f: fmt.get(f, "") for f in libsvc.ALBUM_FIELDS}
    flex = {k: a.get(k) for k in a.keys() if k not in a._fields and k not in libsvc.ALBUM_FIELDS}  # noqa: SIM118
    return render(
        request,
        "library/album.html",
        nav="library",
        album=a,
        items=items,
        fields=fields,
        flex=flex,
        cover=request.app.state.artwork.cover_status(a),
        path=items[0].filepath.parent if items else None,
    )


@router.get("/album/{album_id}/cover")
def album_cover(request: Request, album_id: int, size: int = 0):
    a = _album_or_404(album_id)
    aw = request.app.state.artwork
    p = aw.cover_path(a)
    headers = {"Cache-Control": "private, max-age=300"}
    if p is not None:
        if size:
            try:
                return FileResponse(aw.thumbnail(p, min(size, 1200)), headers=headers)
            except Exception:  # noqa: BLE001 - fall back to the original file
                pass
        return FileResponse(p, headers=headers)
    data = aw.embedded_cover(a)
    if data:
        return Response(data, media_type="image/jpeg", headers=headers)
    return FileResponse(NOCOVER, media_type="image/svg+xml", headers=headers)


def _fields_from_form(form, allowed: list[str], current: dict) -> dict[str, str]:
    out = {}
    for f in allowed:
        if f in form and str(form[f]) != str(current.get(f, "")):
            out[f] = str(form[f])
    out.update(libsvc.parse_extra_fields(str(form.get("extra", ""))))
    return out


@router.post("/album/{album_id}/edit")
async def album_edit(request: Request, album_id: int):
    a = _album_or_404(album_id)
    form = await request.form()
    fmt = a.formatted()
    try:
        fields = _fields_from_form(
            form, libsvc.ALBUM_FIELDS, {f: fmt.get(f, "") for f in libsvc.ALBUM_FIELDS}
        )
    except ValueError as e:
        flash(request, str(e), "warn")
        return redirect(f"/library/album/{album_id}")
    move = bool(form.get("move"))
    if not fields and not move:
        flash(request, "Nothing changed.")
        return redirect(f"/library/album/{album_id}")
    job = request.app.state.jobs.submit(
        "album-edit",
        f"Edit {a.albumartist} - {a.album}",
        {"album_id": album_id, "fields": fields, "write": bool(form.get("write")), "move": move},
        user(request),
    )
    flash(request, f"Edit queued (job #{job}).")
    return redirect(f"/library/album/{album_id}")


@router.post("/album/{album_id}/remove")
def album_remove(request: Request, album_id: int, delete: str = Form("")):
    a = _album_or_404(album_id)
    job = request.app.state.jobs.submit(
        "album-remove",
        f"Remove {a.albumartist} - {a.album}",
        {"album_id": album_id, "delete": bool(delete)},
        user(request),
    )
    flash(request, f"Removal queued (job #{job}).")
    return redirect("/library")


@router.get("/item/{item_id}")
def item_page(request: Request, item_id: int):
    it = _item_or_404(item_id)
    fmt = it.formatted()
    fields = {f: fmt.get(f, "") for f in libsvc.ITEM_FIELDS}
    flex = {k: it.get(k) for k in it.keys() if k not in it._fields}  # noqa: SIM118
    return render(
        request,
        "library/item.html",
        nav="library",
        item=it,
        fields=fields,
        flex=flex,
        album=it.get_album(),
    )


@router.post("/item/{item_id}/edit")
async def item_edit(request: Request, item_id: int):
    it = _item_or_404(item_id)
    form = await request.form()
    fmt = it.formatted()
    try:
        fields = _fields_from_form(
            form, libsvc.ITEM_FIELDS, {f: fmt.get(f, "") for f in libsvc.ITEM_FIELDS}
        )
    except ValueError as e:
        flash(request, str(e), "warn")
        return redirect(f"/library/item/{item_id}")
    move = bool(form.get("move"))
    if not fields and not move:
        flash(request, "Nothing changed.")
        return redirect(f"/library/item/{item_id}")
    job = request.app.state.jobs.submit(
        "item-edit",
        f"Edit {it.artist} - {it.title}",
        {"item_id": item_id, "fields": fields, "write": bool(form.get("write")), "move": move},
        user(request),
    )
    flash(request, f"Edit queued (job #{job}).")
    return redirect(f"/library/item/{item_id}")


@router.post("/item/{item_id}/remove")
def item_remove(request: Request, item_id: int, delete: str = Form("")):
    it = _item_or_404(item_id)
    back = f"/library/album/{it.album_id}" if it.album_id else "/library/tracks"
    job = request.app.state.jobs.submit(
        "item-remove",
        f"Remove {it.artist} - {it.title}",
        {"item_id": item_id, "delete": bool(delete)},
        user(request),
    )
    flash(request, f"Removal queued (job #{job}).")
    return redirect(back)

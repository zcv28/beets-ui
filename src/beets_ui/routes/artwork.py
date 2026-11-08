from __future__ import annotations

import uuid
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from .. import beets_ctx
from ..artwork.service import MAX_IMAGE_BYTES, ArtworkError, validate_image
from ..web import HERE, flash, partial, redirect, render, user

router = APIRouter(prefix="/artwork")
NOARTIST = HERE / "static" / "noartist.svg"


def _album_or_404(album_id: int):
    album = beets_ctx.lib().get_album(album_id)
    if album is None:
        raise HTTPException(404, "album not found")
    return album


async def _store_upload(request: Request, upload: UploadFile | None) -> str | None:
    if upload is None or not upload.filename:
        return None
    data = await upload.read(MAX_IMAGE_BYTES + 1)
    validate_image(data)
    d = request.app.state.settings.data_dir / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    path = d / uuid.uuid4().hex
    path.write_bytes(data)
    return str(path)


def _sources(form_values: list[str]) -> list[str] | None:
    return [s for s in form_values if s] or None


# ------------------------------------------------------------------ covers


@router.get("")
def covers(request: Request, filter: str = "missing", q: str = "", page: int = 1):
    aw = request.app.state.artwork
    lib = beets_ctx.lib()
    try:
        albums = (
            list(lib.albums(q.split() + ["albumartist+", "album+"]))
            if q
            else list(lib.albums(["albumartist+", "album+"]))
        )
    except Exception as e:  # noqa: BLE001
        flash(request, f"Bad query: {e}", "warn")
        albums = []
    rows = []
    for a in albums:
        st = aw.cover_status(a)
        if filter == "missing" and (st["has"] or st["embedded"]):
            continue
        if filter == "nofile" and st["has"]:
            continue  # no cover file, with or without embedded art
        if filter == "low" and (st["has"] or st["embedded"]) and not st["low"]:
            continue  # "low" shows missing and low-resolution covers
        rows.append((a, st))
    per = 60
    page = max(1, page)
    total = len(rows)
    return render(
        request,
        "artwork/covers.html",
        nav="artwork",
        rows=rows[(page - 1) * per : page * per],
        total=total,
        page=page,
        more=total > page * per,
        filter=filter,
        q=q,
        sources=aw.available_sources("album"),
        min_px=request.app.state.settings.min_cover_px,
    )


@router.get("/album/{album_id}")
def album_art(request: Request, album_id: int):
    a = _album_or_404(album_id)
    aw = request.app.state.artwork
    return render(
        request,
        "artwork/album.html",
        nav="artwork",
        album=a,
        cover=aw.cover_status(a),
        sources=aw.available_sources("album"),
    )


@router.get("/album/{album_id}/candidates")
def album_candidates(request: Request, album_id: int):
    a = _album_or_404(album_id)
    sources = _sources(request.query_params.getlist("src"))
    cands = request.app.state.artwork.find_album_covers(a, sources)
    return partial(
        request,
        "artwork/_candidates.html",
        cands=cands,
        action=f"/artwork/album/{album_id}/set",
        hidden={},
        embed=True,
    )


@router.post("/album/{album_id}/set")
async def album_set(
    request: Request,
    album_id: int,
    url: str = Form(""),
    embed: str = Form(""),
    upload: UploadFile | None = File(None),
):
    a = _album_or_404(album_id)
    try:
        up = await _store_upload(request, upload)
    except ArtworkError as e:
        flash(request, str(e), "warn")
        return redirect(f"/artwork/album/{album_id}")
    if not up and not url:
        flash(request, "Choose an image or upload a file.", "wip")
        return redirect(f"/artwork/album/{album_id}")
    job = request.app.state.jobs.submit(
        "cover-set",
        f"Set cover for {a.albumartist} - {a.album}",
        {"album_id": album_id, "url": url, "upload": up, "embed": bool(embed)},
        user(request),
    )
    flash(request, f"Saving cover (job #{job}).")
    return redirect(f"/library/album/{album_id}")


@router.post("/covers/fill")
async def covers_fill(request: Request):
    form = await request.form()
    params = {
        "sources": _sources([str(s) for s in form.getlist("src")]),
        "embed": bool(form.get("embed")),
        "low": bool(form.get("low")),
        "extract": bool(form.get("extract")),
    }
    job = request.app.state.jobs.submit(
        "covers-fill", "Fill missing album covers", params, user(request)
    )
    return redirect(f"/jobs/{job}")


# ----------------------------------------------------------------- artists


@router.get("/artists")
def artists(request: Request, filter: str = "missing", q: str = ""):
    aw = request.app.state.artwork
    results = request.app.state.state.artist_art()
    rows = []
    for a in aw.artists():
        if q and q.casefold() not in a["name"].casefold():
            continue
        info = aw.artist_info(a["name"])
        info["auto"] = results.get(a["name"])
        status = (info["auto"] or {}).get("status")
        if filter == "missing" and (info["image"] or info["folder"] is None):
            continue
        if filter == "review" and not (
            info["folder"] is not None
            and (status in ("ambiguous", "notfound") and not info["image"] or status == "name")
        ):
            continue
        if filter == "nofolder" and info["folder"] is not None:
            continue
        rows.append(info)
    return render(
        request,
        "artwork/artists.html",
        nav="artwork",
        rows=rows[:500],
        total=len(rows),
        filter=filter,
        q=q,
        sources=aw.available_sources("artist"),
        names=request.app.state.settings.artist_image_names,
    )


@router.get("/artist")
def artist_page(request: Request, name: str):
    aw = request.app.state.artwork
    info = aw.artist_info(name)
    if not info["albums"]:
        raise HTTPException(404, "artist not found")
    info["auto"] = request.app.state.state.artist_art().get(name)
    return render(
        request,
        "artwork/artist.html",
        nav="artwork",
        info=info,
        sources=aw.available_sources("artist"),
        names=request.app.state.settings.artist_image_names,
    )


@router.get("/artist/candidates")
def artist_candidates(request: Request, name: str):
    sources = _sources(request.query_params.getlist("src"))
    cands = request.app.state.artwork.find_artist_images(name, sources)
    return partial(
        request,
        "artwork/_candidates.html",
        cands=cands,
        action="/artwork/artist/set",
        hidden={"name": name},
        embed=False,
    )


@router.get("/artist/image")
def artist_image(request: Request, name: str, size: int = 300):
    aw = request.app.state.artwork
    info = aw.artist_info(name)
    headers = {"Cache-Control": "private, max-age=300"}
    if info["image"] is None:
        return FileResponse(NOARTIST, media_type="image/svg+xml", headers=headers)
    try:
        return FileResponse(aw.thumbnail(info["image"], min(size, 1200)), headers=headers)
    except Exception:  # noqa: BLE001
        return FileResponse(info["image"], headers=headers)


@router.post("/artist/set")
async def artist_set(
    request: Request,
    name: str = Form(...),
    url: str = Form(""),
    upload: UploadFile | None = File(None),
):
    back = "/artwork/artist?name=" + quote(name, safe="")
    info = request.app.state.artwork.artist_info(name)
    if info["folder"] is None:
        flash(request, f"No dedicated folder for {name}: {info['reason']}", "warn")
        return redirect(back)
    try:
        up = await _store_upload(request, upload)
    except ArtworkError as e:
        flash(request, str(e), "warn")
        return redirect(back)
    if not up and not url:
        flash(request, "Choose an image or upload a file.", "wip")
        return redirect(back)
    job = request.app.state.jobs.submit(
        "artist-set",
        f"Set image for {name}",
        {"artist": name, "url": url, "upload": up},
        user(request),
    )
    flash(request, f"Saving artist image (job #{job}).")
    return redirect(back)


@router.post("/artists/fill")
async def artists_fill(request: Request):
    form = await request.form()
    params = {
        "sources": _sources([str(s) for s in form.getlist("src")]),
        "overwrite": bool(form.get("overwrite")),
        "retry": bool(form.get("retry")),
    }
    job = request.app.state.jobs.submit(
        "artists-fill", "Fill missing artist images", params, user(request)
    )
    return redirect(f"/jobs/{job}")

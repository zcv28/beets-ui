from __future__ import annotations

import shlex
import unicodedata

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, Response

from .. import artists as artists_svc
from .. import beets_ctx, discography, romanize, upgrades
from .. import library as libsvc
from ..web import HERE, flash, is_fragment, partial, redirect, render, user

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
    if is_fragment(request) and page > 1:
        name = "library/_album_rows.html" if view == "table" else "library/_album_cards.html"
        return partial(request, name, albums=rows, more=more, q=q, sort=sort, page=page, view=view)
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
    if is_fragment(request) and page > 1:
        return partial(
            request,
            "library/_track_rows.html",
            items=rows,
            more=more,
            q=q,
            page=page,
            singletons=singletons,
        )
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


def _albumartist_originals() -> dict[str, str]:
    with beets_ctx.lib().transaction() as tx:
        rows = tx.query(
            "SELECT a.albumartist, MAX(f.value) FROM albums a JOIN album_attributes f "
            "ON f.entity_id = a.id AND f.key = 'albumartist_original' GROUP BY a.albumartist"
        )
    return {name: value for name, value in rows if value and value != name}


def _letter(name: str) -> str:
    """Bucket for the A–Z bar: a Latin letter, 0–9, あ (Japanese/CJK) or #."""
    ch = unicodedata.normalize("NFKD", name.strip()[:1]).upper()[:1]
    if "A" <= ch <= "Z":
        return ch
    if ch.isdigit():
        return "0–9"
    if ch and unicodedata.name(ch, "").startswith(("CJK", "HIRAGANA", "KATAKANA", "HANGUL")):
        return "あ"
    return "#"


@router.get("/artists")
def artists(request: Request):
    rows = request.app.state.artwork.artists()
    originals = _albumartist_originals()
    for a in rows:
        a["letter"] = _letter(a["name"])
        a["original"] = originals.get(a["name"], "")
    order = [*"ABCDEFGHIJKLMNOPQRSTUVWXYZ", "0–9", "あ", "#"]
    present = {a["letter"] for a in rows}
    return render(
        request,
        "library/artists.html",
        nav="library",
        artists=rows,
        letters=[l for l in order if l in present],  # noqa: E741
    )


@router.get("/artist")
def artist_page(request: Request, name: str = ""):
    """One album artist: their albums, and what the library is missing of theirs."""
    if not name:
        return redirect("/library/artists")
    rows, _ = libsvc.albums(shlex.quote(f"albumartist:={name}"), "artist", per_page=1000)
    if not rows:
        raise HTTPException(404, "no albums by that artist")
    state = request.app.state.state
    lib = beets_ctx.lib()
    with lib.transaction() as tx:
        tracks, seconds = tx.query(
            "SELECT COUNT(*), COALESCE(SUM(length), 0) FROM items WHERE albumartist=?", (name,)
        )[0]
    gaps = discography.artist_gaps(lib, state, name)
    wanted = state.wanted_list()
    return render(
        request,
        "library/artist.html",
        nav="library",
        artist=name,
        original=_albumartist_originals().get(name, ""),
        albums=rows,
        tracks=tracks,
        hours=seconds / 3600,
        gaps=gaps,
        has_picture=request.app.state.artwork.artist_info(name)["image"] is not None,
        missing=[(r, rel) for r in gaps for rel in r["missing"]],
        upcoming=[(r, rel) for r in gaps for rel in r["upcoming"]],
        wanted={f"{w['source']}:{w['release_id']}": w for w in wanted if w["kind"] == "missing"},
        get_enabled=getattr(request.app.state, "acquirer", None) is not None,
    )


@router.get("/artists/aliases")
def artists_aliases(request: Request):
    state = request.app.state.state
    return render(
        request,
        "library/artists_aliases.html",
        nav="library",
        groups=artists_svc.alias_suggestions(),
        aliases=sorted(state.artist_aliases().items(), key=lambda a: a[1].casefold()),
    )


@router.post("/artists/merge")
async def artists_merge(request: Request):
    form = await request.form()
    names = [str(n).strip() for n in form.getlist("name") if str(n).strip()]
    target = str(form.get("target", "")).strip() or (names[0] if names else "")
    aliases = [n for n in names if n.casefold() != target.casefold()]
    back = "/library/artists/aliases" if form.get("back") == "aliases" else "/library/artists"
    if not target or not aliases:
        flash(request, "Tick an artist and type a new name, or tick several to merge.", "wip")
        return redirect(back)
    job = request.app.state.jobs.submit(
        "artists-merge",
        f"Merge {', '.join(aliases)} into {target}",
        {
            "target": target,
            "aliases": aliases,
            "write": bool(form.get("write")),
            "move": bool(form.get("move")),
            "tracks": bool(form.get("tracks")),
        },
        user(request),
    )
    return redirect(f"/jobs/{job}")


@router.post("/artists/aliases/delete")
def artists_alias_delete(request: Request, alias: str = Form(...)):
    request.app.state.state.delete_artist_alias(alias)
    flash(request, f"Forgot the alias {alias!r}. Albums already merged keep their artist.")
    return redirect("/library/artists/aliases")


@router.get("/artists/primary")
def artists_primary(request: Request):
    return render(
        request,
        "library/artists_primary.html",
        nav="library",
        rows=artists_svc.proposals(aliases=request.app.state.state.artist_aliases()),
        enabled=request.app.state.settings.primary_artist,
    )


@router.post("/artists/primary")
async def artists_primary_apply(request: Request):
    form = await request.form()
    ids = [int(str(i)) for i in form.getlist("album") if str(i).isdigit()]
    if not ids:
        flash(request, "No albums selected.")
        return redirect("/library/artists/primary")
    n = len(ids)
    job = request.app.state.jobs.submit(
        "artists-primary",
        f"Keep primary album artist ({n} album{'s' if n != 1 else ''})",
        {"album_ids": ids, "write": bool(form.get("write")), "move": bool(form.get("move"))},
        user(request),
    )
    return redirect(f"/jobs/{job}")


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
        romanizable=_romanizable(a),
        path=items[0].filepath.parent if items else None,
        issues=upgrades.album_issues(beets_ctx.lib(), request.app.state.state, album_id),
        reasons=upgrades.REASONS,
        wanted=request.app.state.state.wanted_for_album(album_id),
        get_enabled=getattr(request.app.state, "acquirer", None) is not None,
        length=sum(i.length or 0 for i in items),
        formats=sorted({str(i.format) for i in items}),
    )


# One-click album commands (run through the console job, like typing them there).
ALBUM_ACTIONS = {
    "mbsync": ("mbsync", ["mbsync", "-a"]),
    "fetchart": ("fetchart", ["fetchart", "-f"]),
    "embedart": ("embedart", ["embedart", "-y"]),
    "replaygain": ("replaygain", ["replaygain", "-a"]),
    "lyrics": ("lyrics", ["lyrics"]),
}


@router.post("/album/{album_id}/run")
def album_run(request: Request, album_id: int, action: str = Form(...)):
    a = _album_or_404(album_id)
    if action not in ALBUM_ACTIONS:
        raise HTTPException(400, "unknown action")
    label, argv = ALBUM_ACTIONS[action]
    job = request.app.state.console.submit_argv([*argv, f"album_id:{album_id}"], user(request))
    msg = f"{label} for {a.album} queued as job #{job}."
    if action == "mbsync" and request.app.state.settings.romanize and _romanizable(a):
        # mbsync writes the source's names back: romanize again right after it.
        _queue_romanize(request, [album_id], f"Romanize {a.albumartist} - {a.album}")
        msg += " Names are romanized again afterwards."
    flash(request, msg)
    return redirect(f"/library/album/{album_id}")


def _romanizable(album) -> bool:
    return str(album.get("data_source") or "").lower() in romanize.SUPPORTED


def _queue_romanize(request: Request, album_ids: list[int] | None, title: str, **opts) -> int:
    params = {"album_ids": album_ids, "write": True, "move": True, **opts}
    return request.app.state.jobs.submit("romanize", title, params, user(request))


@router.post("/album/{album_id}/romanize")
def album_romanize(request: Request, album_id: int):
    a = _album_or_404(album_id)
    job = _queue_romanize(request, [album_id], f"Romanize {a.albumartist} - {a.album}")
    return redirect(f"/jobs/{job}")


@router.get("/album/{album_id}/romanize")
def album_romanize_by_hand(request: Request, album_id: int):
    """Names nobody has romanized, to type in: each with a reading to start from."""
    a = _album_or_404(album_id)
    return render(
        request,
        "library/romanize_album.html",
        nav="library",
        album=a,
        rows=romanize.by_hand(a, list(a.items()), beets_ctx.lib()),
        romanizable=_romanizable(a),
    )


@router.post("/album/{album_id}/romanize/names")
async def album_romanize_typed(request: Request, album_id: int):
    a = _album_or_404(album_id)
    form = await request.form()
    names = romanize.typed_names(form, a, list(a.items()))
    job = _queue_romanize(
        request,
        [album_id],
        f"Romanize {a.albumartist} - {a.album} by hand",
        names=names,
        write=bool(form.get("write")),
        move=bool(form.get("move")),
    )
    return redirect(f"/jobs/{job}")


@router.post("/album/{album_id}/lyrics")
def album_lyrics(request: Request, album_id: int, force: str = Form("")):
    a = _album_or_404(album_id)
    params = {"album_ids": [album_id], "force": bool(force)}
    title = f"Synced lyrics for {a.albumartist} - {a.album}"
    job = request.app.state.jobs.submit("lyrics", title, params, user(request))
    return redirect(f"/jobs/{job}")


@router.post("/item/{item_id}/lyrics")
def item_lyrics(request: Request, item_id: int):
    it = _item_or_404(item_id)
    params = {"item_ids": [item_id], "force": True}
    title = f"Synced lyrics for {it.artist} - {it.title}"
    job = request.app.state.jobs.submit("lyrics", title, params, user(request))
    return redirect(f"/jobs/{job}")


@router.get("/romanize")
def romanize_page(request: Request):
    return render(
        request,
        "library/romanize.html",
        nav="library",
        status=romanize.library_status(),
        enabled=request.app.state.settings.romanize,
    )


@router.post("/romanize")
def romanize_run(
    request: Request, write: str = Form(""), move: str = Form(""), only_new: str = Form("")
):
    job = _queue_romanize(
        request,
        None,
        "Romanize names" + (" (albums not done yet)" if only_new else " (whole library)"),
        write=bool(write),
        move=bool(move),
        only_new=bool(only_new),
    )
    return redirect(f"/jobs/{job}")


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

"""Artists and releases as the catalogues know them, whether or not the library has them.

Find's results lead here: an artist's page lists every release of theirs, to get all
or some; a release's page shows its tracklist, to get it. Discover and Coming in
link an artist's name here too.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .. import beets_ctx, discography
from ..catalog import KINDS, NAMES, SOURCES, Catalog, CatalogError, valid
from ..discography import norm
from ..web import flash, partial, redirect, render

router = APIRouter(prefix="/get")


def _catalog(request: Request) -> Catalog:
    return request.app.state.catalog


def _check(source: str, ident: str) -> None:
    if source not in SOURCES or not valid(source, ident):
        raise HTTPException(404, "unknown source or ID")


class Requests:
    """Requests under way or done, to find the one for a release: by its source and ID,
    else by artist and title (the same album asked for from another catalogue, or
    from Missing). The latest request wins."""

    def __init__(self, state):
        rows = [
            w for w in state.wanted_list() if w["status"] != "cancelled" and w["kind"] == "missing"
        ]
        self.by_id = {f"{w['source']}:{w['release_id']}": w for w in rows}
        self.by_name: dict[tuple[str, str], dict] = {}
        for w in rows:
            for ar in {norm(w["artist"]), norm(w["artist_original"] or "")} - {""}:
                for al in {norm(w["album"]), norm(w["album_original"] or "")} - {""}:
                    self.by_name[(ar, al)] = w

    def find(self, rel: dict[str, Any] | None, *artists: str) -> dict | None:
        if not rel:
            return None
        w = self.by_id.get(f"{rel['source']}:{rel['id']}")
        if w:
            return w
        names = {norm(x) for x in _artist_names(rel, *artists)} - {""}
        titles = {norm(x) for x in _titles(rel)} - {""}
        return next(
            (self.by_name[(a, t)] for a in names for t in titles if (a, t) in self.by_name), None
        )


def _artist_names(rel: dict[str, Any], *more: str) -> list[str]:
    """Who a release is by: as credited, each artist in the credit, and any others named.
    (A discography's releases carry ``artists`` as the credit printed, not a list.)"""
    listed = rel.get("artists")
    each = [a["name"] for a in listed] if isinstance(listed, list) else []
    return [rel.get("artist") or "", *each, *more]


def _titles(rel: dict[str, Any]) -> list[str]:
    return rel.get("names") or [rel["title"], rel.get("original") or ""]


def _get_enabled(request: Request) -> bool:
    return getattr(request.app.state, "acquirer", None) is not None


def _library_artists(lib: Any) -> dict[str, str]:
    """Album artists in the library, by their name (romanized or as written) normalised."""
    out: dict[str, str] = {}
    for a in lib.albums():
        for n in (a.albumartist, a.get("albumartist_original") or ""):
            if n:
                out.setdefault(discography.norm(n), a.albumartist)
    return out


def library_artist(lib: Any, source: str, artist_id: str, *names: str) -> str:
    """The library's name for an artist: by the ID its albums were tagged with, else by name."""
    g = discography.library_groups(lib).get((source, artist_id))
    if g:
        return g["artist"]
    known = _library_artists(lib)
    return next((known[discography.norm(n)] for n in names if discography.norm(n) in known), "")


def _owned(index: dict[str, Any], rel: dict[str, Any] | None, *artists: str) -> str:
    """ "yours", "maybe" or "" (see discography.ownership)."""
    if not rel:
        return ""
    return discography.ownership(
        index, [rel["id"]], _artist_names(rel, *artists), _titles(rel), rel.get("date") or ""
    )


def _came_from(request: Request) -> tuple[str, str]:
    """(address, label) of the page that led to this one, for its back link.

    Taken from the page that linked here; kept for the page, so after an action on
    it (which comes back to the page itself) the link still goes where you were.
    """
    here = request.url.path
    kept = dict(request.session.get("came_from") or {})
    ref = _back(request, "")
    if ref and urlsplit(ref).path != here:
        label = _origin_label(request, ref)
        if label:
            kept.pop(here, None)
            kept[here] = [ref, label]
            request.session["came_from"] = dict(list(kept.items())[-20:])
            return ref, label
    got = kept.get(here)
    return (got[0], got[1]) if got else ("/get/find", "Find")


def _origin_label(request: Request, url: str) -> str:
    parts = urlsplit(url)
    path, q = parts.path, parse_qs(parts.query)
    one = lambda k: (q.get(k) or [""])[0]  # noqa: E731
    if path == "/get/find":
        return f"Find: {one('q')}" if one("q") and one("by") != "hand" else "Find"
    fixed = {
        "/": "Home",
        "/get/discover": "Discover",
        "/get/coming": "Coming in",
        "/get/missing": "Missing",
        "/library": "Library",
    }
    if path in fixed:
        return fixed[path]
    if path == "/library/artist" and one("name"):
        return one("name")
    if path.startswith("/get/artist/"):
        return one("name") or "Artist"
    if path.startswith("/get/release/"):
        bits = path.split("/")
        rel = _catalog(request).peek_release(bits[3], bits[4]) if len(bits) > 4 else None
        return rel["title"] if rel else "Release"
    if path.startswith("/wanted/"):
        return "Request"
    if path.startswith("/library/album/"):
        return "Album"
    return ""


def _back(request: Request, fallback: str) -> str:
    """Where the page that linked here was (for an error on the way)."""
    ref = request.headers.get("referer") or ""
    host = request.headers.get("host") or ""
    for prefix in (f"http://{host}", f"https://{host}"):
        if host and ref.startswith(prefix + "/"):
            return ref[len(prefix) :]
    return fallback


# ------------------------------------------------------------------- find


@router.get("/find/lookup")
def find_lookup(request: Request, kind: str = "releases", q: str = "", src: str = "musicbrainz"):
    """One part of Find's results (artists, releases or tracks), loaded on its own."""
    if kind not in KINDS or src not in SOURCES:
        raise HTTPException(404, "unknown kind or source")
    error, found = "", []
    if len(q.strip()) >= 2:
        try:
            found = _catalog(request).search(src, kind, q)
        except CatalogError as e:
            error = str(e)
    lib = beets_ctx.lib()
    if kind == "artists":
        known = _library_artists(lib)
        groups = discography.library_groups(lib)
        for a in found:
            g = groups.get((a["source"], a["id"]))
            a["library"] = (g or {}).get("artist") or known.get(discography.norm(a["name"]), "")
    else:
        index = discography.owned_index(lib)
        requests = Requests(request.app.state.state)
        for r in found:
            rel = r if kind == "releases" else r.get("release")
            r["owned"] = _owned(index, rel)
            r["w"] = requests.find(rel)
            if kind == "tracks" and rel:
                # A track from a compilation (or someone else's album): getting the
                # release would bring much more than the track.
                mine = {a["id"] for a in r.get("artists") or []}
                theirs = {a["id"] for a in rel.get("artists") or []}
                r["elsewhere"] = rel["kind"] == "compilation" or bool(
                    mine and theirs and not mine & theirs
                )
    return partial(
        request,
        "find/_lookup.html",
        kind=kind,
        found=found,
        error=error,
        q=q,
        src=src,
        source_name=NAMES.get(src, src),
        get_enabled=_get_enabled(request),
        sources_enabled=request.app.state.source is not None,
    )


# ----------------------------------------------------------------- artist


@router.get("/artist/of/{source}/{release_id}")
def artist_of(request: Request, source: str, release_id: str):
    """The page of the artist a release is by (Discover and Coming in know only the release)."""
    _check(source, release_id)
    try:
        main = _catalog(request).main_artist(source, release_id)
    except CatalogError as e:
        flash(request, f"Couldn't look up who that release is by: {e}.", "warn")
        return redirect(_back(request, "/get/find"))
    if main is None:
        flash(request, "That release is by various artists.", "wip")
        return redirect(f"/get/release/{source}/{release_id}")
    return redirect(f"/get/artist/{source}/{main['id']}?name={quote(main['name'], safe='')}")


def _picked(kinds: list[str]) -> tuple[str, ...]:
    picked = tuple(k for k in kinds if k in discography.KINDS)
    return picked or discography.DEFAULT_KINDS


@router.get("/artist/{source}/{artist_id}")
def artist_page(
    request: Request,
    source: str,
    artist_id: str,
    name: str = "",
    kinds: list[str] = Query(default=[]),
):
    """An artist as the catalogue knows them; their releases load into the page."""
    _check(source, artist_id)
    cached = request.app.state.state.discography(source, artist_id) or {}
    info: dict[str, Any] = {
        "name": name or cached.get("artist") or "",
        "original": "",
        "detail": "",
    }
    error = ""
    try:
        # Kept a day in memory: the name as the catalogue has it, and who they are.
        info = _catalog(request).artist(source, artist_id)
    except CatalogError as e:
        if not info["name"]:
            error = str(e)
    lib = beets_ctx.lib()
    in_library = library_artist(lib, source, artist_id, info["name"], info.get("original") or "")
    picked = _picked(kinds)
    back_url, back_label = _came_from(request)
    return render(
        request,
        "get/artist.html",
        nav="get",
        back_url=back_url,
        back_label=back_label,
        source=source,
        source_name=NAMES.get(source, source),
        artist_id=artist_id,
        info=info,
        error=error,
        in_library=in_library,
        has_picture=bool(in_library)
        and request.app.state.artwork.artist_info(in_library)["image"] is not None,
        kinds=discography.KINDS,
        picked=picked,
        url=(
            f"{discography.MUSICBRAINZ}/artist/{artist_id}"
            if source == "musicbrainz"
            else f"{discography.VOCADB[source]}/Ar/{artist_id}"
        ),
    )


def artist_releases(
    request: Request, source: str, artist_id: str, name: str, kinds: tuple[str, ...], fresh=False
) -> dict[str, Any]:
    """An artist's releases of ``kinds``, each marked in the library ("yours" or "maybe"),
    asked for, or upcoming; and the library's albums by them that matched none of
    their releases (likely the "maybe" ones, under another name)."""
    releases, looked_up = _catalog(request).discography(source, artist_id, name, fresh=fresh)
    lib = beets_ctx.lib()
    index = discography.owned_index(lib)
    requests = Requests(request.app.state.state)
    today = time.strftime("%Y-%m-%d")
    cached = request.app.state.state.discography(source, artist_id) or {}
    libname = library_artist(lib, source, artist_id, name)
    names = [name, cached.get("artist") or "", libname]
    rows = []
    for rel in releases:
        if rel["kind"] not in kinds:
            continue
        row = {**rel, "source": source, "upcoming": bool(rel["date"] and rel["date"] > today)}
        row["owned"] = discography.ownership(
            index, [rel["id"]], names, rel.get("names") or [rel["title"]], rel["date"]
        )
        row["w"] = requests.find({**row, "artist": name, "artists": []}, *names)
        rows.append(row)
    # Newest first; releases without a date last.
    rows.sort(key=lambda r: r["date"] or "", reverse=True)
    # Get all takes only what is surely not yours: not in the library, not maybe, not
    # out later, and not asked for (a request that failed can be made again).
    gettable = [
        r
        for r in rows
        if not r["owned"] and not r["upcoming"] and (not r["w"] or r["w"]["status"] == "failed")
    ]
    return {
        "rows": rows,
        "gettable": gettable,
        "maybe": [r for r in rows if r["owned"] == discography.MAYBE and not r["upcoming"]],
        "unmatched": _unmatched(lib, libname, releases),
        "library_name": libname,
        "looked_up": looked_up,
        "total": len(releases),
    }


def _unmatched(lib: Any, libname: str, releases: list[dict]) -> list[Any]:
    """The library's albums by an artist that none of the catalogue's releases claims."""
    if not libname:
        return []
    ids = {r["id"] for r in releases}
    titles: set[str] = set()
    for r in releases:
        for n in r.get("names") or [r["title"]]:
            titles |= {discography.norm(n), discography.bare(n)}
    out = []
    for a in lib.albums():
        if a.albumartist != libname:
            continue
        if {str(a.get("mb_releasegroupid") or ""), str(a.get("mb_albumid") or "")} & ids:
            continue
        mine = [t for t in (a.album, a.get("album_original")) if t]
        if {discography.norm(t) for t in mine} & titles or {
            discography.bare(t) for t in mine
        } & titles:
            continue
        out.append(a)
    return sorted(out, key=lambda a: (a.year or 0, a.album))


@router.get("/artist/{source}/{artist_id}/releases")
def artist_releases_part(
    request: Request,
    source: str,
    artist_id: str,
    name: str = "",
    kinds: list[str] = Query(default=[]),
    fresh: int = 0,
):
    _check(source, artist_id)
    picked = _picked(kinds)
    error, data = "", None
    try:
        data = artist_releases(request, source, artist_id, name, picked, fresh=bool(fresh))
    except CatalogError as e:
        error = str(e)
    return partial(
        request,
        "get/_artist_releases.html",
        source=source,
        source_name=NAMES.get(source, source),
        artist_id=artist_id,
        artist_name=name,
        picked=picked,
        data=data,
        error=error,
        get_enabled=_get_enabled(request),
        back=f"/get/artist/{source}/{artist_id}?name={quote(name, safe='')}"
        + "".join(f"&kinds={k}" for k in picked),
    )


# ---------------------------------------------------------------- release


@router.get("/release/{source}/{release_id}")
def release_page(request: Request, source: str, release_id: str):
    _check(source, release_id)
    try:
        rel = _catalog(request).release(source, release_id)
    except CatalogError as e:
        if str(e) == "not found":
            raise HTTPException(404, "release not found") from e
        flash(request, f"Couldn't look that release up: {e}.", "warn")
        return redirect(_back(request, "/get/find"))
    lib = beets_ctx.lib()
    index = discography.owned_index(lib)
    w = Requests(request.app.state.state).find(rel)
    back_url, back_label = _came_from(request)
    return render(
        request,
        "get/release.html",
        nav="get",
        back_url=back_url,
        back_label=back_label,
        rel=rel,
        source_name=NAMES.get(source, source),
        owned=_owned(index, rel),
        w=w,
        upcoming=bool(rel["date"] and rel["date"] > time.strftime("%Y-%m-%d")),
        get_enabled=_get_enabled(request),
    )


@router.get("/release/{source}/{release_id}/tracks")
def release_tracks(request: Request, source: str, release_id: str):
    _check(source, release_id)
    try:
        tracks, error = _catalog(request).tracks(source, release_id), ""
    except CatalogError as e:
        tracks, error = [], str(e)
    if not tracks and not error:
        return HTMLResponse('<p class="hint">No tracklist is known for this release.</p>')
    return partial(request, "get/_tracks.html", tracks=tracks, error=error)

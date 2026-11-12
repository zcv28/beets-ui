"""Download sources. Pages use ``app.state.source`` (or None) and never name it."""

from __future__ import annotations

import hashlib
from urllib.parse import quote

from ..settings import Settings
from .base import Source, SourceError
from .watch import Watcher

__all__ = [
    "Source",
    "SourceError",
    "Watcher",
    "build",
    "coming",
    "folders_in_flight",
    "incoming",
    "requests_in_flight",
]


def build(settings: Settings) -> Source | None:
    """The configured source, if any. One for now; the first configured wins."""
    if settings.slskd_enabled:
        from ..slskd import Slskd
        from .soulseek import SoulseekSource

        return SoulseekSource(Slskd(settings.slskd_url, settings.slskd_api_key))
    return None


def incoming(app) -> list[dict]:
    """Downloads still on their way (or failed), from the watcher's last check.

    Finished ones are left out: by then they are folders in the inbox.
    """
    watch = getattr(app.state, "source_watch", None)
    # Downloads for a request show with the request instead.
    mine = {
        (w["origin"], w["remote_folder"]) for w in app.state.state.wanted_list(("downloading",))
    }
    fetches = app.state.state.meta("fetches") or {}
    return [
        {**r, "fetch": fetches.get(f"{r['origin']}|{r['folder']}")}
        for r in (watch.rows if watch else [])
        if r["state"] != "done" and (r["origin"], r["folder"]) not in mine
    ]


def folders_in_flight(state, watch) -> set[str]:
    """Inbox folders a download is still writing to (their names).

    The inbox leaves these alone: they are not looked up, imported on their own or
    counted as waiting for review until their download has finished.
    """
    names = {
        r["folder_name"]
        for r in (watch.rows if watch else [])
        if r["state"] in ("downloading", "queued") and r.get("folder_name")
    }
    names |= {w["folder"] for w in state.wanted_list(("downloading",)) if w["folder"]}
    # A download from Find between two uploaders: the next one is being looked for.
    for f in (state.meta("fetches") or {}).values():
        if f.get("searching") and f.get("folder_name"):
            names.add(f["folder_name"])
    return names


def requests_in_flight(app) -> list[dict]:
    """Requests not yet in the library (or failed), with download progress where known."""
    from ..acquire import OPEN

    watch = getattr(app.state, "source_watch", None)
    rows = {(r["origin"], r["folder"]): r for r in (watch.rows if watch else [])}
    out = app.state.state.wanted_list(OPEN)
    for w in out:
        t = rows.get((w["origin"], w["remote_folder"])) if w["status"] == "downloading" else None
        w["progress"] = t["percent"] if t else None
        w["transfer"] = t
    return out


# Coming in lists what needs you first, then what is moving, then what waits its turn.
_ORDER = {"needs": 0, "moving": 1, "waiting": 2}


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _request_item(w: dict) -> dict:
    from ..acquire import ASKS_YOU

    wid = w["id"]
    item = {
        "kind": "request",
        "key": f"w{wid}",
        "title": w["album"],
        "artist": w["artist"],
        "group": "needs"
        if w["status"] in ASKS_YOU
        else ("waiting" if w["status"] == "queued" else "moving"),
        "w": w,
        "tone": "",
        "reason": "",
        "primary": None,
    }
    from ..acquire import reason

    if w["status"] == "choosing":
        item.update(tone="warning", reason=reason(w))
        if w["offers"]:
            item["primary"] = {"label": "Pick a copy", "href": f"/wanted/{wid}#copies"}
        else:
            item["primary"] = {"label": "Search again", "post": f"/wanted/{wid}/retry"}
    elif w["status"] == "failed":
        item.update(
            tone="destructive",
            reason=w["status_text"],
            primary={"label": "Search again", "post": f"/wanted/{wid}/retry"},
        )
    elif w["status"] == "review":
        item.update(tone="info", reason=w["status_text"])
        if w["folder"]:
            item["primary"] = {
                "label": "Review",
                "href": "/inbox/folder?f=" + quote(w["folder"], safe=""),
            }
    return item


def _download_item(r: dict) -> dict:
    f = r.get("fetch") or {}
    failed = r["state"] in ("failed", "cancelled")
    item = {
        "kind": "download",
        "key": "d" + hashlib.md5(f"{r['origin']}|{r['folder']}".encode()).hexdigest()[:12],
        "title": f.get("album") or r.get("folder_name") or r["folder"],
        "artist": f.get("artist") or "",
        "group": "needs"
        if f.get("stuck") or failed
        else ("moving" if r["state"] == "downloading" else "waiting"),
        "r": r,
        "tone": "",
        "reason": "",
        "primary": None,
    }
    if f.get("stuck"):
        item.update(tone="warning", reason=_capital(f["stuck"]))
        if f.get("search_id"):
            item["primary"] = {
                "label": "Pick a different copy",
                "href": f"/get/find/s/{f['search_id']}",
            }
    elif failed:
        n = len(r.get("failed_files") or [])
        item.update(
            tone="destructive",
            reason=f"{n} file{'s' if n != 1 else ''} failed from {r['origin']}"
            if n
            else r.get("label", ""),
        )
    return item


def coming(app) -> list[dict]:
    """Everything on its way into the library, one entry per release.

    A request carries its download (if one is running); a download started by hand
    from Find is its own entry, named by the release its search was for when known.
    Each entry: kind ("request" or "download"), key, title, artist, group ("needs":
    it stopped until you act; "moving"; "waiting"), tone and reason (why it needs you),
    primary (its one main action: {"label", "href"} or {"label", "post"}), and ``w``
    (the request) or ``r`` (the download row). Home's "Needs you" reads the same groups.
    """
    items = [_request_item(w) for w in requests_in_flight(app)]
    items += [_download_item(r) for r in incoming(app)]
    items.sort(key=lambda i: _ORDER[i["group"]])
    return items

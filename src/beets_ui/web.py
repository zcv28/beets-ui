"""Shared web helpers: templates, flash messages, small formatters."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from . import __version__
from .history import UNDOABLE
from .lyrics import grouped, is_synced

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


def fmt_duration(seconds: float | int | None) -> str:
    s = int(seconds or 0)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(n: float | int | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_time(ts: float | None) -> str:
    if not ts:
        return ""
    d = dt.datetime.fromtimestamp(ts)
    if time.time() - ts < 86400:
        return d.strftime("%H:%M:%S")
    return d.strftime("%Y-%m-%d %H:%M")


def match_class(pct: float | None) -> str:
    """Status token for a match confidence: 90% or more is a confident match."""
    if pct is None:
        return "destructive"
    if pct >= 90:
        return "success"
    if pct >= 75:
        return "warning"
    return "destructive"


_KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")


def lang_attr(text: Any) -> Markup:
    """` lang="ja"` (or ko) for names in their own script, so they are read and drawn right.

    Han without kana is taken as Japanese too: this library is mostly Japanese releases.
    """
    s = str(text or "")
    if _KANA.search(s) or _HAN.search(s):
        return Markup(' lang="ja"')
    if _HANGUL.search(s):
        return Markup(' lang="ko"')
    return Markup("")


def fsdecode(v: Any) -> str:
    return os.fsdecode(v) if isinstance(v, bytes) else str(v or "")


templates.env.filters.update(
    duration=fmt_duration,
    size=fmt_size,
    ts=fmt_time,
    match_class=match_class,
    q=lambda s: quote(str(s), safe=""),
    # One beets query term, quoted so names with spaces stay one term.
    term=lambda s: shlex.quote(str(s)),
    fsdecode=fsdecode,
    lang=lang_attr,
    uid=lambda s: hashlib.md5(str(s).encode()).hexdigest()[:12],
)
templates.env.globals["version"] = __version__
templates.env.globals["undoable"] = UNDOABLE
templates.env.globals["synced"] = is_synced
templates.env.globals["lyric_lines"] = grouped


def flash(request: Request, message: str, kind: str = "msg") -> None:
    msgs = request.session.get("flash", [])
    msgs.append([kind, message])
    request.session["flash"] = msgs[-5:]


def render(request: Request, name: str, status_code: int = 200, **ctx: Any):
    app = request.app
    flashes = request.session.pop("flash", []) if "session" in request.scope else []
    base = {
        "request": request,
        "user": request.session.get("user") if "session" in request.scope else None,
        "flashes": flashes,
        "sources_enabled": getattr(app.state, "source", None) is not None,
        "source_status": getattr(app.state, "source_watch", None) and app.state.source_watch.status,
        "source_label": getattr(getattr(app.state, "source", None), "label", ""),
        "nav": ctx.pop("nav", ""),
        "running": len(app.state.jobs.running()),
        "pending": app.state.jobs.pending(),
        "since": time.time(),
        "theme": app.state.settings.theme,
    }
    return templates.TemplateResponse(request, name, {**base, **ctx}, status_code=status_code)


def partial(request: Request, name: str, **ctx: Any):
    return templates.TemplateResponse(request, name, {"request": request, **ctx})


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def is_fragment(request: Request) -> bool:
    """An htmx request for part of a page.

    Boosted navigation sends HX-Request too, but wants the whole page.
    """
    h = request.headers
    return bool(h.get("hx-request")) and not h.get("hx-boosted")


def toast(
    title: str,
    text: str = "",
    kind: str = "success",
    undo: dict[str, Any] | None = None,
    body: str = "",
) -> HTMLResponse:
    """An answer to an in-place action: the row swaps itself, a pop-up says what happened.

    ``undo`` is ``{"url": ..., "vals": {...}}``; the pop-up's Undo button posts it.
    """
    detail: dict[str, Any] = {"title": title, "text": text, "kind": kind}
    if undo:
        detail["undo"] = undo
    resp = HTMLResponse(body)
    resp.headers["HX-Trigger"] = json.dumps({"toast": detail})
    return resp


def user(request: Request) -> str | None:
    return request.session.get("user")

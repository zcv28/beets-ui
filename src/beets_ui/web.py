"""Shared web helpers: templates, flash messages, small formatters."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import __version__

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
    if pct is None:
        return "bad"
    if pct >= 90:
        return "good"
    if pct >= 75:
        return "ok"
    return "bad"


def fsdecode(v: Any) -> str:
    return os.fsdecode(v) if isinstance(v, bytes) else str(v or "")


templates.env.filters.update(
    duration=fmt_duration,
    size=fmt_size,
    ts=fmt_time,
    match_class=match_class,
    q=lambda s: quote(str(s), safe=""),
    fsdecode=fsdecode,
    uid=lambda s: hashlib.md5(str(s).encode()).hexdigest()[:12],
)
templates.env.globals["version"] = __version__


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
        "slskd_enabled": app.state.settings.slskd_enabled,
        "nav": ctx.pop("nav", ""),
        "running": app.state.jobs.current,
        "pending": app.state.jobs.pending(),
    }
    return templates.TemplateResponse(request, name, {**base, **ctx}, status_code=status_code)


def partial(request: Request, name: str, **ctx: Any):
    return templates.TemplateResponse(request, name, {"request": request, **ctx})


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def user(request: Request) -> str | None:
    return request.session.get("user")

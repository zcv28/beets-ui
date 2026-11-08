from __future__ import annotations

from fastapi import APIRouter, Form, Request

from ..auth import AuthError, authenticate
from ..web import redirect, render

router = APIRouter()


def _safe_next(url: str | None) -> str:
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url:
        return "/inbox"
    return url


@router.get("/login")
def login_form(request: Request, next: str = "/inbox"):
    if request.session.get("user"):
        return redirect(_safe_next(next))
    s = request.app.state.settings
    return render(request, "login.html", next=_safe_next(next), ldap=s.ldap_enabled, error=None)


@router.post("/login")
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/inbox"),
):
    s = request.app.state.settings
    limiter = request.app.state.limiter
    key = request.client.host if request.client else "?"
    error = None
    if limiter.blocked(key):
        error = "Too many failed attempts, try again in a few minutes."
    else:
        try:
            who = authenticate(s, username, password)
        except AuthError as e:
            who, error = None, str(e)
        if who:
            limiter.reset(key)
            request.session.clear()
            request.session["user"] = who
            return redirect(_safe_next(next))
        limiter.fail(key)
        error = error or "Invalid credentials."
    return render(
        request,
        "login.html",
        status_code=401,
        next=_safe_next(next),
        ldap=s.ldap_enabled,
        error=error,
        username=username,
    )


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")

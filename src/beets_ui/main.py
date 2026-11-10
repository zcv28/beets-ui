"""Application factory and entry point."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, beets_ctx, discography, history, listening, lyrics, romanize, sources
from . import artists as artists_svc
from .artwork.service import Artwork
from .auth import RateLimiter
from .console import Console
from .inbox import Inbox
from .jobs import JobQueue
from .library import register_jobs
from .settings import Settings
from .state import State
from .web import HERE

log = logging.getLogger("beets_ui")

PUBLIC_PREFIXES = ("/login", "/static/", "/healthz")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # Cookies are SameSite=Lax; additionally reject cross-origin form posts.
            origin = request.headers.get("origin")
            if origin and origin != "null":
                host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
                if urlsplit(origin).netloc != host.split(",")[0].strip():
                    return PlainTextResponse("cross-origin request rejected", status_code=403)
        if path.startswith(PUBLIC_PREFIXES) or request.session.get("user"):
            return await call_next(request)
        if request.headers.get("hx-request"):
            resp = PlainTextResponse("login required", status_code=401)
            resp.headers["HX-Redirect"] = "/login"
            return resp
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


def create_app(settings: Settings | None = None, background: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    beets_ctx.init(settings.beetsdir)

    state = State(settings.state_db)
    jobs = JobQueue(state)
    inbox = Inbox(settings, state, jobs)
    console = Console(jobs, str(settings.beetsdir))
    artwork = Artwork(settings, jobs)
    register_jobs(jobs)
    artists_svc.register_jobs(jobs, settings.artist_image_names)
    history.register_jobs(jobs, settings.artist_image_names)
    romanize.register_jobs(jobs, settings)
    discography.register_jobs(jobs)
    listening.register_jobs(jobs, settings)
    lyrics.register_jobs(jobs, settings)

    source = sources.build(settings)
    source_watch = sources.Watcher(source) if source else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if background:
            jobs.start()
            inbox.start_watcher()
            if source_watch:
                source_watch.start()
        yield
        if background:
            if source_watch:
                source_watch.stop()
            inbox.stop_watcher()
            jobs.stop()

    app = FastAPI(
        title="beets-ui",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.state = state
    app.state.jobs = jobs
    app.state.inbox = inbox
    app.state.console = console
    app.state.artwork = artwork
    app.state.source = source
    app.state.source_watch = source_watch
    app.state.limiter = RateLimiter()

    # Order matters: the session middleware must wrap the auth middleware.
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.ensure_secret_key(),
        session_cookie="beetsui_session",
        max_age=30 * 24 * 3600,
        same_site="lax",
        https_only=os.environ.get("BEETSUI_SECURE_COOKIES", "").lower() in ("1", "true", "yes"),
    )
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    from .routes import artwork as r_artwork
    from .routes import auth as r_auth
    from .routes import console as r_console
    from .routes import events as r_events
    from .routes import find as r_find
    from .routes import inbox as r_inbox
    from .routes import insights as r_insights
    from .routes import jobs as r_jobs
    from .routes import library as r_library

    for r in (
        r_auth,
        r_inbox,
        r_library,
        r_console,
        r_jobs,
        r_find,
        r_artwork,
        r_insights,
        r_events,
    ):
        app.include_router(r.router)
    app.include_router(r_find.legacy)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        # Served from the root so it may control every page; it only passes requests through.
        return FileResponse(
            HERE / "static" / "sw.js",
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/", include_in_schema=False)
    def index():
        return RedirectResponse("/inbox", status_code=303)

    return app


def run() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    port = int(os.environ.get("BEETSUI_PORT", "8337"))
    uvicorn.run(
        "beets_ui.main:create_app",
        factory=True,
        host=os.environ.get("BEETSUI_HOST", "0.0.0.0"),
        port=port,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
        # Open live-update streams would otherwise hold a restart up.
        timeout_graceful_shutdown=3,
    )


if __name__ == "__main__":
    run()

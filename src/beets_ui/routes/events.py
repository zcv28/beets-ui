"""Live updates: one server-sent event stream per open tab.

The server checks a cheap version of each topic every second and sends an event
when it changes. Parts of a page marked ``data-refresh="<topic>"`` then reload
themselves (app.js), instead of every list polling on its own timer.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from .inbox import _version as inbox_version

router = APIRouter()

TICK = 1.0  # seconds between checks
PING = 15  # seconds between keep-alive comments, so proxies keep the line open
# A stream ends after this long and the browser reconnects at once. This bounds
# a stream whose client vanished without the server noticing.
LIFETIME = 300


def versions(app) -> dict[str, str]:
    state = app.state.state
    watch = getattr(app.state, "source_watch", None)
    return {
        "jobs": state.jobs_version(),
        "inbox": inbox_version(state.folders()),
        "downloads": watch.version if watch else "",
        "wanted": state.wanted_version(),
    }


@router.get("/events")
async def events(request: Request):
    app = request.app
    # A reconnecting browser sends the last id it saw: things may have changed
    # while it was away, so every topic is announced once.
    resumed = bool(request.headers.get("last-event-id"))

    async def stream():
        yield f"retry: 3000\nid: {int(time.time())}\n\n"
        last: dict[str, str] = {}
        started = time.monotonic()
        quiet = 0.0
        first = True
        while time.monotonic() - started < LIFETIME:
            if await request.is_disconnected():
                return
            now = await run_in_threadpool(versions, app)
            out = []
            for topic, v in now.items():
                if (first and resumed) or (not first and last.get(topic) != v):
                    out.append(f"event: {topic}\nid: {int(time.time())}\ndata: {v}\n\n")
            last, first = now, False
            if out:
                quiet = 0.0
                yield "".join(out)
            else:
                quiet += TICK
                if quiet >= PING:
                    quiet = 0.0
                    yield ": ping\n\n"
            await asyncio.sleep(TICK)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

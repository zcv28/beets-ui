"""Download sources. Pages use ``app.state.source`` (or None) and never name it."""

from __future__ import annotations

from ..settings import Settings
from .base import Source, SourceError
from .watch import Watcher

__all__ = ["Source", "SourceError", "Watcher", "build", "incoming", "requests_in_flight"]


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
    return [
        r
        for r in (watch.rows if watch else [])
        if r["state"] != "done" and (r["origin"], r["folder"]) not in mine
    ]


def requests_in_flight(app) -> list[dict]:
    """Requests not yet in the library (or failed), with download progress where known."""
    from ..acquire import OPEN

    watch = getattr(app.state, "source_watch", None)
    rows = {(r["origin"], r["folder"]): r for r in (watch.rows if watch else [])}
    out = app.state.state.wanted_list(OPEN)
    for w in out:
        t = rows.get((w["origin"], w["remote_folder"])) if w["status"] == "downloading" else None
        w["progress"] = t["percent"] if t else None
    return out

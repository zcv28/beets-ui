"""Download sources. Pages use ``app.state.source`` (or None) and never name it."""

from __future__ import annotations

from ..settings import Settings
from .base import Source, SourceError
from .watch import Watcher

__all__ = ["Source", "SourceError", "Watcher", "build", "incoming"]


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
    return [r for r in (watch.rows if watch else []) if r["state"] != "done"]

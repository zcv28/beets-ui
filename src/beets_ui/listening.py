"""Listening stats from Jellyfin play counts.

A job reads every audio track Jellyfin knows with the user's play data
(play count, last played, favourite) and matches it to the beets library
by its path inside the music folder. Jellyfin and beets can see the music
under different mount points (``/data/music`` vs ``/music/imported``), so
the longest path tail that names a beets track wins.

Only Jellyfin's running totals are available (no history), so the stats are
about all time: never played, not played in a year, most played.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import random
import re
import time
from collections import defaultdict
from typing import Any

import httpx

from . import __version__, beets_ctx
from .jobs import JobContext, JobQueue
from .settings import Settings
from .state import State

log = logging.getLogger("beets_ui.listening")

SYNC_KIND = "jellyfin-sync"
STALE_AFTER = 6 * 3600
YEAR = 365 * 86400
PAGE = 2000


class JellyfinError(RuntimeError):
    pass


class Jellyfin:
    def __init__(self, url: str, api_key: str, http: httpx.Client | None = None):
        self.url = url.rstrip("/")
        auth = (
            f'MediaBrowser Client="beets-ui", Device="beets-ui", DeviceId="beets-ui", '
            f'Version="{__version__}", Token="{api_key}"'
        )
        self.http = http or httpx.Client(timeout=60)
        self.headers = {"Authorization": auth, "Accept": "application/json"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            r = self.http.get(f"{self.url}{path}", params=params, headers=self.headers)
        except httpx.HTTPError as e:
            raise JellyfinError(f"cannot reach Jellyfin at {self.url}: {e}") from e
        if r.status_code == 401:
            raise JellyfinError("Jellyfin rejected the API key (JELLYFIN_API_KEY)")
        if r.status_code >= 400:
            raise JellyfinError(f"Jellyfin answered {r.status_code} for {path}")
        return r.json()

    def user(self, name: str = "") -> dict[str, Any]:
        users = self._get("/Users")
        if name:
            for u in users:
                if u.get("Name", "").casefold() == name.casefold():
                    return u
            raise JellyfinError(f"no Jellyfin user named {name!r} (JELLYFIN_USER)")
        admins = [u for u in users if (u.get("Policy") or {}).get("IsAdministrator")]
        if len(users) == 1 or admins:
            return users[0] if len(users) == 1 else admins[0]
        raise JellyfinError("several Jellyfin users: set JELLYFIN_USER to pick one")

    def tracks(self, user_id: str):
        start = 0
        while True:
            data = self._get(
                "/Items",
                {
                    "userId": user_id,
                    "IncludeItemTypes": "Audio",
                    "Recursive": "true",
                    "Fields": "Path",
                    "EnableImages": "false",
                    "EnableUserData": "true",
                    "StartIndex": start,
                    "Limit": PAGE,
                },
            )
            items = data.get("Items") or []
            yield from items
            start += len(items)
            if not items or start >= int(data.get("TotalRecordCount") or 0):
                return


def parse_date(value: str | None) -> float | None:
    if not value:
        return None
    # Jellyfin writes up to 7 fractional digits; Python reads at most 6.
    value = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.UTC)
    return d.timestamp()


def _parts(path: str) -> list[str]:
    return [p for p in path.replace("\\", "/").split("/") if p]


class PathMatcher:
    """Finds the beets track for a path another program sees."""

    def __init__(self, lib: Any):
        root = _parts(os.fsdecode(lib.directory))
        self.by_rel: dict[str, int] = {}
        with lib.transaction() as tx:
            rows = tx.query("SELECT id, path FROM items")
        for item_id, raw in rows:
            parts = _parts(os.fsdecode(raw))
            if parts[: len(root)] == root:
                parts = parts[len(root) :]
            self.by_rel["/".join(parts)] = item_id

    def match(self, path: str) -> int | None:
        """The longest tail of ``path`` that is a track's path inside the library."""
        parts = _parts(path)
        for k in range(len(parts)):
            hit = self.by_rel.get("/".join(parts[k:]))
            if hit is not None:
                return hit
        return None


def sync(lib: Any, state: State, client: Jellyfin, user: str = "", log_fn=None) -> dict:
    say = log_fn or (lambda _msg: None)
    u = client.user(user)
    say(f"Reading play counts of Jellyfin user {u.get('Name')}")
    matcher = PathMatcher(lib)
    found: dict[int, tuple[int, float | None, bool]] = {}
    total = unmatched = 0
    examples: list[str] = []
    for t in client.tracks(u["Id"]):
        total += 1
        item_id = matcher.match(t.get("Path") or "")
        if item_id is None:
            unmatched += 1
            if len(examples) < 5 and t.get("Path"):
                examples.append(t["Path"])
            continue
        ud = t.get("UserData") or {}
        count = int(ud.get("PlayCount") or 0)
        last = parse_date(ud.get("LastPlayedDate"))
        fav = bool(ud.get("IsFavorite"))
        old = found.get(item_id)
        if old:  # the same file in two Jellyfin libraries
            count += old[0]
            last = max(filter(None, (last, old[1])), default=None)
            fav |= old[2]
        found[item_id] = (count, last, fav)
    state.replace_plays([(i, c, last, fav) for i, (c, last, fav) in found.items()])
    info = {
        "at": time.time(),
        "user": u.get("Name"),
        "total": total,
        "matched": len(found),
        "unmatched": unmatched,
        "examples": examples,
    }
    state.set_meta("jellyfin_sync", info)
    say(f"Matched {len(found)} of {total} Jellyfin tracks to the library")
    for p in examples:
        say(f"  not in the library: {p}")
    return info


def sync_due(state: State) -> bool:
    info = state.meta("jellyfin_sync") or {}
    if time.time() - float(info.get("at") or 0) < STALE_AFTER:
        return False
    return not any(j["status"] in ("queued", "running") for j in state.jobs(5, kind=SYNC_KIND))


# ------------------------------------------------------------------ stats


def stats(lib: Any, state: State, now: float | None = None, limit: int = 30) -> dict[str, Any]:
    now = now or time.time()
    plays = state.plays()
    with lib.transaction() as tx:
        albums = {
            r[0]: {
                "id": r[0],
                "albumartist": r[1],
                "album": r[2],
                "year": r[3],
                "added": r[4],
                "tracks": 0,
                "played": 0,
                "plays": 0,
                "last": None,
                "favorites": 0,
            }
            for r in tx.query("SELECT id, albumartist, album, year, added FROM albums")
        }
        items = tx.query("SELECT id, album_id, title, artist, length FROM items")
    tracks = []
    artists: dict[str, dict[str, Any]] = defaultdict(lambda: {"plays": 0, "tracks": 0})
    total_plays = 0
    seconds = 0.0
    played_tracks = 0
    for item_id, album_id, title, artist, length in items:
        p = plays.get(item_id) or {}
        count = int(p.get("count") or 0)
        total_plays += count
        seconds += count * float(length or 0)
        played_tracks += count > 0
        if count:
            tracks.append(
                {"id": item_id, "title": title, "artist": artist, "plays": count, "album": album_id}
            )
        a = albums.get(album_id)
        if a is None:
            continue
        a["tracks"] += 1
        a["played"] += count > 0
        a["plays"] += count
        a["favorites"] += bool(p.get("favorite"))
        if p.get("last_played") and (a["last"] is None or p["last_played"] > a["last"]):
            a["last"] = p["last_played"]
    for a in albums.values():
        if a["plays"]:
            art = artists[a["albumartist"]]
            art["plays"] += a["plays"]
            art["tracks"] += a["played"]
    al = [a for a in albums.values() if a["tracks"]]
    never = sorted((a for a in al if not a["plays"]), key=lambda a: a["added"] or 0)
    stale = sorted(
        (a for a in al if a["plays"] and a["last"] and a["last"] < now - YEAR),
        key=lambda a: a["last"],
    )
    top_albums = sorted((a for a in al if a["plays"]), key=lambda a: -a["plays"])
    top_artists = sorted(artists.items(), key=lambda kv: -kv[1]["plays"])
    tracks.sort(key=lambda t: -t["plays"])
    return {
        "albums": len(al),
        "albums_played": sum(1 for a in al if a["plays"]),
        "tracks": len(items),
        "tracks_played": played_tracks,
        "plays": total_plays,
        "hours": seconds / 3600,
        "favorites": sum(a["favorites"] for a in al),
        "never": never[:limit],
        "never_count": len(never),
        "stale": stale[:limit],
        "stale_count": len(stale),
        "top_albums": top_albums[:limit],
        "top_artists": [{"name": n, **v} for n, v in top_artists[:limit]],
        "top_tracks": tracks[:limit],
        "never_ids": [a["id"] for a in never],
    }


def random_unplayed(lib: Any, state: State) -> int | None:
    ids = stats(lib, state, limit=0)["never_ids"]
    return random.choice(ids) if ids else None


# -------------------------------------------------------------------- job


def register_jobs(jobs: JobQueue, settings: Settings, client_factory=None) -> None:
    @jobs.register(SYNC_KIND, lane="net")
    def jellyfin_sync(ctx: JobContext) -> None:
        if not settings.jellyfin_enabled:
            raise JellyfinError("set JELLYFIN_URL and JELLYFIN_API_KEY first")
        client = (client_factory or Jellyfin)(settings.jellyfin_url, settings.jellyfin_api_key)
        sync(beets_ctx.lib(), ctx.state, client, settings.jellyfin_user, ctx.log)

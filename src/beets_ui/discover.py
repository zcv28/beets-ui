"""Discover: albums ListenBrainz thinks you'd like, and new releases by artists you listen to.

A job reads the user's newest "created for you" playlists (Weekly Exploration,
Weekly Jams, Daily Jams), turns their tracks into albums with ListenBrainz's
metadata lookup, and reads the user's fresh releases. All of it is public, so
only the user name is needed. The result is cached; what you already own,
dismissed or asked for is left out when the page is shown, so it follows the
library without another sync.

Albums are MusicBrainz release groups, which the Get engine already knows how
to fetch a tracklist for.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

from .discography import Client, DiscographyError, norm
from .jobs import JobContext, JobQueue
from .settings import Settings
from .state import State

log = logging.getLogger("beets_ui.discover")

API = "https://api.listenbrainz.org"
SYNC_KIND = "listenbrainz-sync"
STALE_AFTER = 6 * 3600
META_KEY = "listenbrainz"
DISMISS_KIND = "discover"
INTERVAL = 0.5  # seconds between requests to ListenBrainz
FRESH_DAYS = 90  # the most fresh_releases allows
BATCH = 40  # recordings per metadata lookup

# The playlists ListenBrainz makes for a user, by their "source_patch", in the
# order they are worth reading: exploration is new to you, jams mostly aren't.
PLAYLISTS = {
    "weekly-exploration": "Weekly Exploration",
    "weekly-jams": "Weekly Jams",
    "daily-jams": "Daily Jams",
}
# Release group types, as Missing names them.
KINDS = {"Album": "album", "EP": "ep", "Single": "single", "Broadcast": "other", "Other": "other"}
DEFAULT_KINDS = ("album", "ep")


class ListenBrainzError(RuntimeError):
    pass


def _mbid(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _patch(playlist: dict[str, Any]) -> str:
    ext = (playlist.get("extension") or {}).get("https://musicbrainz.org/doc/jspf#playlist") or {}
    meta = (ext.get("additional_metadata") or {}).get("algorithm_metadata") or {}
    return meta.get("source_patch") or ""


def _get(client: Client, path: str, params: dict[str, Any], missing: str = "") -> Any:
    try:
        return client.get(f"{API}{path}", params, interval=INTERVAL)
    except DiscographyError as e:
        # The shared client reports any 404 as a missing artist.
        if str(e) == "artist not found":
            raise ListenBrainzError(missing or "ListenBrainz: not found") from e
        raise ListenBrainzError(f"ListenBrainz: {e}") from e
    except Exception as e:  # noqa: BLE001 - one message for the page, whatever went wrong
        raise ListenBrainzError(f"ListenBrainz: {e}") from e


def playlists(client: Client, user: str) -> list[dict[str, Any]]:
    """The newest playlist of each kind made for the user."""
    data = _get(
        client,
        f"/1/user/{quote(user, safe='')}/playlists/createdfor",
        {"count": 25},
        missing=f"ListenBrainz has no user named {user}",
    )
    newest: dict[str, dict[str, Any]] = {}
    for p in data.get("playlists") or []:
        pl = p.get("playlist") or {}
        patch = _patch(pl)
        if patch in PLAYLISTS and (
            patch not in newest or (pl.get("date") or "") > (newest[patch].get("date") or "")
        ):
            newest[patch] = pl
    return [newest[k] for k in PLAYLISTS if k in newest]


def suggestions(client: Client, user: str, say=lambda _m: None) -> tuple[list, list]:
    """(albums from the playlists, the playlists read) — albums best first."""
    read = []
    tracks: list[tuple[str, str, str, str]] = []  # recording, title, artist, playlist label
    for pl in playlists(client, user):
        label = PLAYLISTS[_patch(pl)]
        try:
            full = _get(client, f"/1/playlist/{_mbid(pl.get('identifier') or '')}", {})
        except ListenBrainzError as e:  # an expired playlist: the others still count
            say(f"{pl.get('title') or label}: skipped ({e})")
            continue
        items = (full.get("playlist") or {}).get("track") or []
        read.append({"label": label, "title": pl.get("title") or label, "date": pl.get("date")})
        say(f"{pl.get('title') or label}: {len(items)} tracks")
        for t in items:
            ids = [_mbid(i) for i in (t.get("identifier") or []) if "/recording/" in i]
            if ids:
                tracks.append((ids[0], t.get("title") or "", t.get("creator") or "", label))

    releases: dict[str, dict[str, Any]] = {}
    ids = list(dict.fromkeys(rec for rec, *_ in tracks))
    for i in range(0, len(ids), BATCH):
        part = ids[i : i + BATCH]
        data = _get(
            client, "/1/metadata/recording/", {"recording_mbids": ",".join(part), "inc": "release"}
        )
        for rec, info in (data or {}).items():
            rel = (info or {}).get("release") or {}
            if rel.get("release_group_mbid"):
                releases[rec] = rel

    albums: dict[str, dict[str, Any]] = {}
    order = list(PLAYLISTS.values())
    for rec, title, artist, label in tracks:
        rel = releases.get(rec)
        if not rel:
            continue
        rg = rel["release_group_mbid"]
        a = albums.setdefault(
            rg,
            {
                "id": rg,
                "release_id": rel.get("mbid") or "",
                "title": rel.get("name") or "",
                "artist": rel.get("album_artist_name") or artist,
                "year": str(rel.get("year") or ""),
                "tracks": [],
                "lists": [],
            },
        )
        if not any(t["title"] == title for t in a["tracks"]):
            a["tracks"].append({"title": title, "artist": artist})
        if label not in a["lists"]:
            a["lists"].append(label)
    ranked = sorted(
        albums.values(),
        key=lambda a: (min(order.index(x) for x in a["lists"]), -len(a["tracks"]), a["title"]),
    )
    return ranked, read


def fresh(client: Client, user: str) -> list[dict[str, Any]]:
    """New and upcoming releases by artists the user listens to, newest first."""
    data = _get(
        client,
        f"/1/user/{quote(user, safe='')}/fresh_releases",
        {"days": FRESH_DAYS, "future": "true"},
    )
    out = []
    for r in ((data or {}).get("payload") or {}).get("releases") or []:
        if not r.get("release_group_mbid"):
            continue
        primary = r.get("release_group_primary_type") or "Other"
        secondary = r.get("release_group_secondary_type") or ""
        out.append(
            {
                "id": r["release_group_mbid"],
                "release_id": r.get("release_mbid") or "",
                "title": r.get("release_name") or "",
                "artist": r.get("artist_credit_name") or "",
                "date": r.get("release_date") or "",
                "type": primary,
                "kind": "compilation"
                if "Compilation" in secondary
                else KINDS.get(primary, "other"),
                "secondary": secondary,
            }
        )
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def sync(state: State, client: Client, user: str, say=lambda _m: None) -> dict[str, Any]:
    say(f"Reading ListenBrainz for {user}")
    albums, read = suggestions(client, user, say)
    releases = fresh(client, user)
    info = {"at": time.time(), "user": user, "playlists": read, "albums": albums, "fresh": releases}
    state.set_meta(META_KEY, info)
    say(f"{len(albums)} suggested albums, {len(releases)} fresh releases")
    return info


def sync_due(state: State) -> bool:
    info = state.meta(META_KEY) or {}
    if time.time() - float(info.get("at") or 0) < STALE_AFTER:
        return False
    return not any(j["status"] in ("queued", "running") for j in state.jobs(5, kind=SYNC_KIND))


# ---------------------------------------------------------------- the page


def _owned(lib: Any) -> tuple[set[str], set[tuple[str, str]]]:
    """What the library has: release group IDs, and (artist, album) name pairs."""
    groups: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for a in lib.albums():
        if a.get("mb_releasegroupid"):
            groups.add(str(a["mb_releasegroupid"]))
        names = {norm(a.album), norm(a.get("album_original") or "")} - {""}
        artists = {norm(a.albumartist), norm(a.get("albumartist_original") or "")} - {""}
        pairs |= {(ar, n) for ar in artists for n in names}
    return groups, pairs


def view(lib: Any, state: State, kinds: tuple[str, ...] = DEFAULT_KINDS) -> dict[str, Any]:
    """The cached result, without what you own or dismissed, with what you asked for."""
    info = state.meta(META_KEY) or {}
    groups, pairs = _owned(lib)
    dismissed = state.dismissed(DISMISS_KIND)
    wanted = {
        w["release_id"]: w
        for w in state.wanted_list()
        if w["source"] == "musicbrainz" and w["status"] != "cancelled"
    }

    def keep(a: dict[str, Any]) -> bool:
        if a["id"] in groups or a["id"] in dismissed:
            return False
        return (norm(a["artist"]), norm(a["title"])) not in pairs

    def mark(a: dict[str, Any]) -> dict[str, Any]:
        return {**a, "wanted": wanted.get(a["id"])}

    today = time.strftime("%Y-%m-%d")
    releases = [mark(r) for r in info.get("fresh") or [] if keep(r) and r["kind"] in kinds]
    return {
        "synced": info.get("at"),
        "user": info.get("user"),
        "playlists": info.get("playlists") or [],
        "albums": [mark(a) for a in info.get("albums") or [] if keep(a)],
        "released": [r for r in releases if r["date"] <= today],
        "upcoming": sorted((r for r in releases if r["date"] > today), key=lambda r: r["date"]),
        "dismissed": len(dismissed),
    }


# -------------------------------------------------------------------- job


def register_jobs(jobs: JobQueue, settings: Settings, client_factory=None) -> None:
    @jobs.register(SYNC_KIND, lane="net")
    def listenbrainz_sync(ctx: JobContext) -> None:
        if not settings.listenbrainz_user:
            raise ListenBrainzError("set LISTENBRAINZ_USER first")
        client = (client_factory or Client)()
        sync(ctx.state, client, settings.listenbrainz_user, ctx.log)

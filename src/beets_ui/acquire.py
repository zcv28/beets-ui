"""Get: find, download and import an album you asked for.

A request (a row in the ``wanted`` table) moves through these states:

    queued       waiting for its turn to search (at most two search at once)
    searching    a search is running at the download source
    choosing     no copy was good enough to take unasked: you pick one
    downloading  the chosen copy is downloading
    arrived      the download finished; waiting for its inbox folder
    lookup       the inbox looks the folder up, by the release that was asked for
    importing    the inbox imports it
    review       it is in the inbox, waiting for you (low match, duplicate, ...)
    done | failed | cancelled

The Acquirer (a background thread) drives queued → arrived. The inbox drives
lookup → done, through ``inbox_*`` hooks below, so a requested folder is still an
ordinary inbox folder you can review.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from typing import Any

from . import discography
from .rank import audio_count, rank
from .sources import Source, SourceError
from .state import State

log = logging.getLogger(__name__)

# Labels and status tokens for each state (the inbox, gaps and request pages).
STATES = {
    "queued": ("running", "Waiting to search"),
    "searching": ("running", "Searching"),
    "choosing": ("warning", "Choose a copy"),
    "downloading": ("running", "Downloading"),
    "arrived": ("running", "Downloaded"),
    "lookup": ("running", "Looking up"),
    "importing": ("running", "Importing"),
    "review": ("warning", "Waiting for review"),
    "done": ("done", "In your library"),
    "failed": ("failed", "Couldn't get it"),
    "cancelled": ("", "Cancelled"),
}
DRIVEN = ("queued", "searching", "downloading", "arrived")
OPEN = (*DRIVEN, "choosing", "lookup", "importing", "review", "failed")
NEEDS_YOU = ("choosing", "review", "failed")
MAX_SEARCHES = 2
MAX_ATTEMPTS = 3
SEARCH_WAIT = 30  # seconds a request's search waits for answers
# A finished download whose folder never shows up in the inbox gives up after this.
ARRIVAL_TIMEOUT = 600


def label(w: dict[str, Any]) -> tuple[str, str]:
    return STATES.get(w["status"], ("", w["status"]))


# ------------------------------------------------------------------ requests


def from_gap(state: State, row: dict[str, Any], rel: dict[str, Any]) -> tuple[int, bool]:
    """A request for a missing release. Returns (id, created): an open request is reused."""
    old = state.wanted_for_release(row["source"], rel["id"])
    if old and old["status"] in OPEN and old["status"] != "failed":
        return old["id"], False
    wid = state.add_wanted(
        kind="missing",
        artist=row["artist"],
        artist_original=row.get("artist_original") or "",
        album=rel["title"],
        album_original=rel.get("original") or "",
        year=(rel.get("date") or "")[:4],
        source=row["source"],
        release_id=rel["id"],
        lookup_id=rel["id"] if row["source"] in discography.VOCADB else "",
    )
    return wid, True


LOSSLESS = {"flac", "alac", "wav", "aiff", "ape", "wavpack", "dsf"}


def from_album(state: State, album: Any) -> tuple[int, bool]:
    """A request for a better copy of an album in the library (it replaces the old one)."""
    old = state.wanted_for_album(album.id)
    if old and old["status"] in OPEN and old["status"] != "failed":
        return old["id"], False
    items = list(album.items())
    lossy = [i for i in items if str(i.format).lower() not in LOSSLESS]
    rate = min((int(i.bitrate or 0) // 1000 for i in lossy), default=0)
    src = str(album.get("data_source") or "").lower()
    lookup = str(album.mb_albumid or "")
    wid = state.add_wanted(
        kind="upgrade",
        artist=album.albumartist,
        artist_original=album.get("albumartist_original") or "",
        album=album.album,
        album_original=album.get("album_original") or "",
        year=str(album.year or ""),
        source=src if src in discography.VOCADB else "musicbrainz",
        release_id=lookup,
        lookup_id=lookup,
        album_id=album.id,
        # Lossy now: any lossless copy, or a lossy one with a higher bitrate.
        min_bitrate=rate if lossy else 0,
        want_lossless=not lossy,
        # An incomplete album's own tracks are not the full tracklist: rank by name then.
        tracks=None
        if _incomplete(album, items)
        else [{"title": i.title, "length": round(i.length or 0)} for i in items],
    )
    return wid, True


def _incomplete(album: Any, items: list) -> bool:
    per_disc: dict[int, int] = {}
    for i in items:
        per_disc[i.disc or 1] = max(per_disc.get(i.disc or 1, 0), int(i.tracktotal or 0))
    return sum(per_disc.values()) > len(items)


# ----------------------------------------------------------------- tracklists


def tracklist(client: discography.Client, source: str, release_id: str) -> tuple[list, str]:
    """(tracks, id to look the album up by on import) for a release from discographies."""
    if source in discography.VOCADB:
        base = discography.VOCADB[source]
        data = client.get(
            f"{base}/api/albums/{release_id}", {"fields": "Tracks", "lang": "Romaji"}, interval=0.5
        )
        tracks = [
            {
                "title": t.get("name") or "",
                "length": (t.get("song") or {}).get("lengthSeconds") or 0,
            }
            for t in data.get("tracks") or []
        ]
        return tracks, release_id
    # MusicBrainz discographies list release groups: pick its usual release.
    data = client.get(
        f"{discography.MUSICBRAINZ}/ws/2/release",
        {"release-group": release_id, "inc": "media+recordings", "fmt": "json", "limit": 50},
        interval=1.1,
    )
    releases = [
        r for r in data.get("releases") or [] if (r.get("status") or "Official") == "Official"
    ]
    if not releases:
        return [], ""
    counts = Counter(
        sum(len(m.get("tracks") or []) for m in r.get("media") or []) for r in releases
    )
    usual = counts.most_common(1)[0][0]
    best = min(
        (
            r
            for r in releases
            if sum(len(m.get("tracks") or []) for m in r.get("media") or []) == usual
        ),
        key=lambda r: r.get("date") or "9999",
    )
    tracks = [
        {"title": t.get("title") or "", "length": round((t.get("length") or 0) / 1000)}
        for m in best.get("media") or []
        for t in m.get("tracks") or []
    ]
    return tracks, best["id"]


def queries(w: dict[str, Any]) -> list[str]:
    """Search texts to try in turn: romanized names, then the names as written."""

    def clean(s: str) -> str:
        # Soulseek drops words starting with "-" and matches whole words only.
        return " ".join(re.sub(r"[^\w\s]+", " ", s).split())

    out: list[str] = []
    for artist, album in (
        (w["artist"], w["album"]),
        (w["artist_original"] or w["artist"], w["album_original"] or w["album"]),
        (w["artist"], w["album_original"]),
    ):
        q = clean(f"{artist} {album}")
        if album and len(q) >= 3 and q.casefold() not in (x.casefold() for x in out):
            out.append(q)
    return out


# ------------------------------------------------------------------- engine


class Acquirer:
    def __init__(
        self,
        state: State,
        source: Source,
        inbox_folder_exists=None,
        transfers=None,
        on_arrival=None,
        client_factory=discography.Client,
        interval: float = 5,
    ):
        self.state = state
        self.source = source
        # Callables, so tests and the app can wire them: does an inbox folder of this
        # name exist, and the latest transfer rows (the watcher's cache).
        self.inbox_folder_exists = inbox_folder_exists or (lambda name: False)
        self.transfers = transfers or source.transfers
        # Called with the folder name when a download has arrived: the inbox looks it up.
        self.on_arrival = on_arrival or (lambda name: None)
        self.client_factory = client_factory
        self.interval = interval
        self._client: discography.Client | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()

    @classmethod
    def build(cls, state: State, inbox: Any, settings: Any, source: Source, watch: Any) -> Acquirer:
        """The app's acquirer: inbox folders and the watcher's download rows."""

        def folder_ready(name: str) -> bool:
            """Is the download's folder in the inbox (scanning once if it just appeared)?"""
            if state.folder(name) is None and (settings.inbox / name).is_dir():
                inbox.scan()
            return state.folder(name) is not None

        def arrived(name: str) -> None:
            row = state.folder(name)
            if row and row["status"] not in ("queued", "previewing", "importing"):
                inbox.queue_preview(name, auto=True, **inbox._wanted_search(row))

        return cls(
            state,
            source,
            inbox_folder_exists=folder_ready,
            transfers=lambda: (watch.rows if watch else []) or source.transfers(),
            on_arrival=arrived,
        )

    # --------------------------------------------------------------- helpers

    def _set(self, w: dict[str, Any], status: str, text: str = "", **fields: Any) -> None:
        self.state.update_wanted(w["id"], status=status, status_text=text, **fields)
        w.update(status=status, status_text=text, **fields)

    @property
    def client(self) -> discography.Client:
        if self._client is None:
            self._client = self.client_factory()
        return self._client

    def wake(self) -> None:
        """Run a step soon (after a new request, a choice, a cancel)."""
        self._wake.set()

    # ------------------------------------------------------------------ loop

    def tick(self) -> None:
        with self._lock:
            rows = self.state.wanted_list(DRIVEN)
            searching = sum(1 for w in rows if w["status"] == "searching")
            for w in rows:
                try:
                    if w["status"] == "queued":
                        if searching >= MAX_SEARCHES:
                            continue
                        if self._start_search(w):
                            searching += 1
                    elif w["status"] == "searching":
                        self._searched(w)
                    elif w["status"] == "downloading":
                        self._downloading(w)
                    elif w["status"] == "arrived":
                        self._arrived(w)
                except SourceError as e:
                    # The source is down or refused: try again on a later tick.
                    self.state.update_wanted(w["id"], error=str(e))
                except Exception as e:  # a bug must not stop the other requests
                    log.exception("request %s", w["id"])
                    self._set(w, "failed", "Something went wrong", error=str(e))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("acquirer")
            self._wake.wait(self.interval)
            self._wake.clear()

    def start(self) -> None:
        threading.Thread(target=self._run, name="beets-ui-get", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ----------------------------------------------------------------- steps

    def _start_search(self, w: dict[str, Any]) -> bool:
        if w["tracks"] is None and w["kind"] == "missing" and w["release_id"]:
            try:
                tracks, lookup = tracklist(self.client, w["source"], w["release_id"])
            except Exception as e:  # unknown tracklist: rank by names, more strictly
                log.info("tracklist for request %s: %s", w["id"], e)
                tracks, lookup = [], w["lookup_id"]
            self.state.update_wanted(w["id"], tracks=tracks, lookup_id=lookup or w["lookup_id"])
            w.update(tracks=tracks, lookup_id=lookup or w["lookup_id"])
        texts = queries(w)
        if w["query_idx"] >= len(texts):
            self._give_up(w)
            return False
        text = texts[w["query_idx"]]
        sid = self.source.search(text, wait=SEARCH_WAIT)
        self._set(w, "searching", f"Searching for {text}", search_id=sid, error=None)
        return True

    def _give_up(self, w: dict[str, Any]) -> None:
        if w["offers"]:
            self._set(w, "choosing", "No copy was clearly right: choose one")
        else:
            self._set(w, "failed", "Nothing found")

    def _searched(self, w: dict[str, Any]) -> None:
        info = self.source.search_info(w["search_id"])
        if info["running"]:
            return
        tried = set(w["tried"] or [])
        picks = [
            p
            for p in rank(self.source.offers(w["search_id"]), w)
            if f"{p.offer['origin']}|{p.offer['folder']}" not in tried
        ]
        # Keep the best few for choosing by hand, whatever happens next.
        keep = [self._slim(p) for p in picks[:5]]
        offers = keep or w["offers"]
        self.state.update_wanted(w["id"], offers=offers)
        w["offers"] = offers
        for p in picks:
            if not p.confident:
                break
            if self._fetch(w, p.offer):
                return
        # Nothing to take unasked: the next search text, or ask.
        if w["query_idx"] + 1 < len(queries(w)):
            self._set(w, "queued", "Trying another search", query_idx=w["query_idx"] + 1)
        else:
            self._give_up(w)

    @staticmethod
    def _slim(p) -> dict[str, Any]:
        o = p.offer
        # The files go along, for when the source can't send the full listing later.
        keys = (
            "origin",
            "folder",
            "path",
            "folder_name",
            "files",
            "format",
            "bitrate",
            "lossless",
            "tracks",
            "size",
            "free_slot",
            "queue",
            "speed",
            "locked",
            "source",
        )
        return {
            **{k: o.get(k) for k in keys},
            "score": round(p.score, 1),
            "why": p.why,
            "confident": p.confident,
        }

    def _fetch(self, w: dict[str, Any], offer: dict[str, Any], check: bool = True) -> bool:
        """Download an offer's whole folder. False if its full listing rules it out."""
        key = f"{offer['origin']}|{offer['folder']}"
        tried = [*(w["tried"] or []), key]
        try:
            files = self.source.listing(offer["origin"], offer["path"])
        except SourceError:
            files = []
        if check and files and w["tracks"] and audio_count(files) < len(w["tracks"]):
            self.state.update_wanted(w["id"], tried=tried)
            w["tried"] = tried
            return False
        if not files:
            files = offer.get("files") or []
        if not files:
            self.state.update_wanted(w["id"], tried=tried)
            w["tried"] = tried
            return False
        self.source.fetch(offer["origin"], files)
        self._set(
            w,
            "downloading",
            f"Downloading from {offer['origin']}",
            origin=offer["origin"],
            remote_folder=offer["folder"],
            folder=offer["folder_name"],
            tried=tried,
            attempts=w["attempts"] + 1,
        )
        return True

    def choose(self, w: dict[str, Any], origin: str, folder: str) -> bool:
        """You picked a copy: download it, whatever its score."""
        with self._lock:
            offer = next(
                (o for o in w["offers"] if o["origin"] == origin and o["folder"] == folder), None
            )
            if offer is None:
                return False
            return self._fetch(w, offer, check=False)

    def _downloading(self, w: dict[str, Any]) -> None:
        if w["folder"] and self.inbox_folder_exists(w["folder"]):
            self.state.link_folder(w["folder"], w["id"])
        row = next(
            (
                r
                for r in self.transfers()
                if r["origin"] == w["origin"] and r["folder"] == w["remote_folder"]
            ),
            None,
        )
        if row is None:
            # Gone from the list (cleared): it counts as arrived if its folder is there.
            if self.inbox_folder_exists(w["folder"]):
                self._set(w, "arrived", "Downloaded")
            return
        if row["state"] == "done":
            self._set(w, "arrived", "Downloaded")
        elif row["state"] in ("failed", "cancelled"):
            self._next_offer(w, row["label"])
        else:
            pct = row["percent"]
            text = (
                f"Downloading, {pct:.0f}%"
                if row["state"] == "downloading"
                else "Queued at the uploader"
            )
            if row.get("place"):
                text += f", place {row['place']}"
            if text != w["status_text"]:
                self.state.update_wanted(w["id"], status_text=text)

    def _next_offer(self, w: dict[str, Any], reason: str) -> None:
        if w["attempts"] < MAX_ATTEMPTS:
            tried = set(w["tried"] or [])
            for o in w["offers"]:
                fresh = f"{o['origin']}|{o['folder']}" not in tried
                if o.get("confident") and fresh and self._fetch(w, o):
                    return
        if w["offers"]:
            self._set(w, "choosing", f"The download failed ({reason}): choose another copy")
        else:
            self._set(w, "failed", f"The download failed ({reason})")

    def _arrived(self, w: dict[str, Any]) -> None:
        if self.inbox_folder_exists(w["folder"]):
            self.state.link_folder(w["folder"], w["id"])
            self._set(w, "lookup", "In the inbox, being looked up")
            self.on_arrival(w["folder"])
        elif time.time() - w["updated_at"] > ARRIVAL_TIMEOUT:
            self._set(
                w, "failed", f"The download finished, but no inbox folder {w['folder']} appeared"
            )


# -------------------------------------------------------------- inbox hooks
# The inbox calls these as a requested folder is looked up and imported.


def lookup_ids(w: dict[str, Any]) -> list[str]:
    return [w["lookup_id"]] if w.get("lookup_id") else []


def inbox_previewed(state: State, w: dict[str, Any], auto_import: bool, why: str = "") -> None:
    if auto_import:
        state.update_wanted(w["id"], status="importing", status_text="Importing")
    else:
        text = f"In the inbox: {why}" if why else "In the inbox, waiting for you"
        state.update_wanted(w["id"], status="review", status_text=text)


def inbox_imported(state: State, w: dict[str, Any], ok: bool, why: str = "") -> None:
    if ok:
        state.update_wanted(w["id"], status="done", status_text="In your library", error=None)
    else:
        state.update_wanted(w["id"], status="review", status_text=f"In the inbox: {why}")

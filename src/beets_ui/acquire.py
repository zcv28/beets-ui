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

It keeps at it before asking you: every search text in turn, the copies that look
right (uploaders that answer with the folder's listing first), then another round
of searches that waits longer for answers. A release with a romanized name and one
as written is searched under each before a copy is taken, so the best copy wins
whichever name its folder uses. Only when the rounds run out does a
request stop at "choosing" (copies were found, none clearly right) or "failed"
(nothing found), where you can keep trying, choose a copy, or find it by hand.
Each request keeps a short history of what was tried.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from typing import Any

from . import beets_ctx, discography
from .rank import audio_count, for_search, rank
from .sources import Source, SourceError
from .state import State

log = logging.getLogger(__name__)

# Labels and status tokens for each state (the inbox, gaps and request pages).
STATES = {
    "queued": ("running", "Waiting to search"),
    "searching": ("running", "Searching"),
    "choosing": ("warning", "Waiting for your pick"),
    "downloading": ("running", "Downloading"),
    "arrived": ("running", "Downloaded"),
    "lookup": ("running", "Looking up"),
    "importing": ("running", "Importing"),
    "review": ("running", "Waiting for review"),
    "done": ("done", "In your library"),
    "failed": ("failed", "Couldn't get it"),
    "cancelled": ("", "Cancelled"),
}
DRIVEN = ("queued", "searching", "downloading", "arrived")
# Requests a download started by hand (Find) can take over.
ADOPTABLE = ("queued", "searching", "choosing", "failed", "cancelled")
OPEN = (*DRIVEN, "choosing", "lookup", "importing", "review", "failed")
NEEDS_YOU = ("choosing", "review", "failed")
# Requests whose folder is in the inbox, being looked up, imported or waiting for review.
IN_INBOX = ("lookup", "importing", "review")
# Requests you can stop: everything but a lookup or an import that is running.
CANCELLABLE = ("queued", "searching", "choosing", "downloading", "arrived", "review")
# Requests that stop until you act on them here (review is decided in the inbox).
ASKS_YOU = ("choosing", "failed")
MAX_SEARCHES = 2
MAX_ATTEMPTS = 6  # downloads started for one request before it asks you
MAX_RESEARCH = 2  # rounds of searches after the first, each waiting longer for answers
SEARCH_WAIT = 30  # seconds a request's first round of searches waits for answers
MAX_LISTINGS = 6  # copies that look right whose listing is asked for, per search
KEEP_OFFERS = 6  # the best copies found so far, kept with the request
HISTORY_KEEP = 60  # entries kept in a request's history
# A finished download whose folder never shows up in the inbox gives up after this.
ARRIVAL_TIMEOUT = 600


def label(w: dict[str, Any]) -> tuple[str, str]:
    return STATES.get(w["status"], ("", w["status"]))


def detail(w: dict[str, Any]) -> str:
    """What a request's status text adds to its label ("" when it only repeats it)."""
    text = (w.get("status_text") or "").strip()
    name = label(w)[1]
    if not text or text == name:
        return ""
    if text.startswith(name + ", "):
        return text[len(name) + 2 :]
    return text


def note(state: State, w: dict[str, Any], text: str) -> None:
    """Add a line to a request's history (what was searched, downloaded, given up on)."""
    history = [*(w.get("history") or []), {"at": time.time(), "text": text}][-HISTORY_KEEP:]
    state.update_wanted(w["id"], history=history)
    w["history"] = history


def search_wait(w: dict[str, Any]) -> int:
    """Seconds a search waits for answers: longer on each round."""
    return SEARCH_WAIT * (1 + (w.get("researched") or 0))


# The one path a release takes: 0 missing, 1 getting, 2 review, 3 in your library.
STAGES = {
    "queued": 1,
    "searching": 1,
    "choosing": 1,
    "downloading": 1,
    "arrived": 1,
    "lookup": 2,
    "importing": 2,
    "review": 2,
    "done": 3,
}


def stage(w: dict[str, Any]) -> tuple[int, bool]:
    """Where a request is on the path, and whether it stopped there (failed or cancelled)."""
    if w["status"] in STAGES:
        return STAGES[w["status"]], False
    return (2 if w.get("folder") else 1), True


# ------------------------------------------------------------------ requests


def from_gap(state: State, row: dict[str, Any], rel: dict[str, Any]) -> tuple[int, bool]:
    """A request for a missing release. Returns (id, created): an open request is reused."""
    old = state.wanted_for_release(row["source"], rel["id"])
    if old and old["status"] in OPEN and old["status"] != "failed":
        return old["id"], False
    # The same album asked for already, from another catalogue or under the other name.
    artists = {discography.norm(row["artist"]), discography.norm(row.get("artist_original") or "")}
    albums = {discography.norm(rel["title"]), discography.norm(rel.get("original") or "")}
    for w in state.wanted_list(tuple(s for s in OPEN if s != "failed")):
        if w["kind"] != "missing":
            continue
        theirs = {discography.norm(w["artist"]), discography.norm(w["artist_original"] or "")}
        named = {discography.norm(w["album"]), discography.norm(w["album_original"] or "")}
        if (artists & theirs) - {""} and (albums & named) - {""}:
            return w["id"], False
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


BRACKETS = re.compile(r"\s*[(\[{（【〔「][^)\]}）】〕」]*[)\]}）】〕」]")


def queries(w: dict[str, Any]) -> list[str]:
    """Search texts to try in turn: romanized names, then the names as written, then
    without bracketed parts ("(Remastered)", "[Disc 1]"), then the album alone when
    its name is distinctive enough to be found by itself."""
    return _queries(w)[0]


def spellings(w: dict[str, Any]) -> int:
    """How many of the first search texts are the release's own name in another spelling
    (romanized, as written). They are all searched before a copy is taken: the same
    album is shared under either name, and the best copy may be under the other one."""
    return _queries(w)[1]


def _queries(w: dict[str, Any]) -> tuple[list[str], int]:

    def clean(s: str) -> str:
        # Soulseek drops words starting with "-" and matches whole words only.
        return " ".join(re.sub(r"[^\w\s]+", " ", s).split())

    def bare(s: str) -> str:
        return BRACKETS.sub(" ", s or "")

    artist, album = w["artist"], w["album"]
    artist_o, album_o = w["artist_original"] or artist, w["album_original"] or album
    out: list[str] = []

    def add(q: str, album_part: str) -> None:
        q = clean(q)
        if album_part and len(q) >= 3 and q.casefold() not in (x.casefold() for x in out):
            out.append(q)

    for a, b in ((artist, album), (artist_o, album_o), (artist, w["album_original"])):
        add(f"{a} {b}", b)
    names = len(out)
    for a, b in ((artist, bare(album)), (artist_o, bare(album_o))):
        add(f"{a} {b}", clean(b))
    for b in dict.fromkeys((clean(bare(album)), clean(bare(album_o)))):
        if distinctive(b):
            add(b, b)
    return out, names


def distinctive(title: str) -> bool:
    """Is an album name unusual enough to search for without its artist?"""
    wide = sum(1 for c in title if ord(c) >= 0x2E80)  # CJK and kana
    return wide >= 4 or (len(title) >= 12 and len(title.split()) >= 2)


# ------------------------------------------------------------------- engine


class Acquirer:
    def __init__(
        self,
        state: State,
        source: Source,
        inbox_folder_exists=None,
        transfers=None,
        on_arrival=None,
        discard_folder=None,
        inbox_files=None,
        client_factory=discography.Client,
        interval: float = 5,
        stall_minutes: float = 15,
    ):
        self.state = state
        self.source = source
        # Callables, so tests and the app can wire them: does an inbox folder of this
        # name exist, and the latest transfer rows (the watcher's cache).
        self.inbox_folder_exists = inbox_folder_exists or (lambda name: False)
        self.transfers = transfers or source.transfers
        # Called with the folder name when a download has arrived: the inbox looks it up.
        self.on_arrival = on_arrival or (lambda name: None)
        # Called with the inbox folder of a copy that was given up on part way.
        self.discard_folder = discard_folder or (lambda name: None)
        # File names already in an inbox folder (a fallback copy only fetches the rest).
        self.inbox_files = inbox_files or (lambda name: set())
        # A download with no progress for this long is given up on (0: never).
        self.stall_after = stall_minutes * 60
        self.client_factory = client_factory
        self.interval = interval
        self._client: discography.Client | None = None
        self._lock = threading.Lock()
        # Downloads from Find you asked another copy of, until the loop gets to them:
        # "origin|folder" -> folder name. (The loop may hold _lock for a while.)
        self._skips: dict[str, str] = {}
        self._skips_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._settled = 0.0  # when requests were last checked against the library

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

        def discard(name: str) -> None:
            """The part of an abandoned copy that reached the inbox, unless it is in use."""
            row = state.folder(name)
            if row and row["status"] not in ("queued", "previewing", "importing", "imported"):
                try:
                    inbox.delete_source(name)
                except OSError as e:
                    log.warning("could not remove the partial download %s: %s", name, e)

        def files_in(name: str) -> set[str]:
            folder = settings.inbox / name
            if not name or not folder.is_dir():
                return set()
            return {p.name.casefold() for p in folder.rglob("*") if p.is_file()}

        return cls(
            state,
            source,
            inbox_folder_exists=folder_ready,
            inbox_files=files_in,
            transfers=lambda: (watch.rows if watch else []) or source.transfers(),
            on_arrival=arrived,
            discard_folder=discard,
            stall_minutes=settings.stall_minutes,
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
            try:
                self._watch_fetches()
            except SourceError as e:
                log.info("watching downloads: %s", e)
            except Exception:
                log.exception("watching downloads")

    # ------------------------------------------------ downloads from Find

    def _watch_fetches(self) -> None:
        """Downloads started by hand: switch to the same folder from someone else when the
        uploader rejects it or stalls, or you ask for another copy. Nobody else has it, even
        after searching again: mark it stuck for you to choose."""
        fetches = self.state.meta(FETCHES_KEY) or {}
        with self._skips_lock:
            skips, self._skips = self._skips, {}
        for key, name in skips.items():
            origin = key.split("|", 1)[0]
            f = fetches.setdefault(key, new_fetch(name, "", name, 0))
            # Asking again after it got stuck searches again.
            f.update(skip=f"you skipped {origin}", stuck="", searching="", researched=0)
        if not fetches:
            return
        rows = {f"{r['origin']}|{r['folder']}": r for r in self.transfers()}
        requested = {f"{w['origin']}|{w['remote_folder']}" for w in self.state.wanted_list(DRIVEN)}
        now = time.time()
        changed = bool(skips)
        for key, f in list(fetches.items()):
            row = rows.get(key)
            if row is None or row["state"] in ("done", "cancelled") or key in requested:
                # Finished, cleared or cancelled by you: forget it after a while.
                if now - f.get("at", now) > FETCH_KEEP:
                    del fetches[key]
                    changed = True
                continue
            if f.get("searching"):
                # Searched again for other uploaders: once answers are in, look at them.
                try:
                    running = self.source.search_info(f["search_id"])["running"]
                except SourceError:
                    running = False
                if running:
                    continue
                reason = f.pop("searching")
            elif f.get("skip"):
                reason = f.pop("skip")
            elif f.get("stuck"):
                continue
            elif row["state"] == "failed":
                label = row["label"][:1].lower() + row["label"][1:]
                reason = f"{label} from {row['origin']}"
            elif moved(row, f.get("progress_bytes", 0), f.get("progress_place")):
                f.update(
                    progress_bytes=row.get("transferred", 0),
                    progress_place=row.get("place"),
                    progress_at=now,
                )
                changed = True
                continue
            else:
                reason = self.giving_up(row, f.get("progress_at"))
            if not reason:
                if row.get("place") is not None and f.get("progress_place") is None:
                    f["progress_place"] = row["place"]
                    changed = True
                continue
            new_key = self._switch(f, row, reason)
            if new_key:
                fetches[new_key] = fetches.pop(key)
            changed = True
        if changed:
            self.state.set_meta(FETCHES_KEY, fetches)

    def another(self, origin: str, folder: str, folder_name: str) -> None:
        """You asked for another copy of a download from Find. The loop switches to the
        next uploader with the same folder (searching again if need be); until then the
        current one keeps going."""
        with self._skips_lock:
            self._skips[f"{origin}|{folder}"] = folder_name
        self.wake()

    def _switch(self, f: dict[str, Any], row: dict[str, Any], reason: str) -> str:
        """Fetch the rest of the same folder from another uploader. Its key, or "" if
        nobody else has it (the download is then marked stuck)."""
        tried = {*f.get("tried", []), f"{row['origin']}|{row['folder']}"}
        tried_origins = {k.split("|", 1)[0] for k in tried}
        offers = []
        if f.get("search_id"):
            try:
                offers = self.source.offers(f["search_id"])
            except SourceError:
                offers = []
        same = [
            o
            for o in offers
            if o["origin"] not in tried_origins
            and discography.norm(o["folder_name"]) == discography.norm(f["folder_name"])
            and (not f.get("tracks") or int(o.get("tracks") or 0) == f["tracks"])
        ]
        for o in for_search(same, f.get("text", "")):
            try:
                files = self.source.listing(o["origin"], o["path"]) or o.get("files") or []
            except SourceError:
                files = o.get("files") or []
            have = self.inbox_files(f["folder_name"])
            rest = [x for x in files if file_name(x["filename"]).casefold() not in have]
            if not rest:
                continue
            ids = [x["id"] for x in row.get("files") or [] if x.get("kind") in ("queued", "active")]
            try:
                if ids:
                    self.source.cancel(row["origin"], ids, remove=True)
                self.source.fetch(o["origin"], rest)
            except SourceError as e:
                log.info("switching %s: %s", f["folder_name"], e)
                continue
            f.update(
                at=time.time(),
                progress_bytes=0,
                progress_at=time.time(),
                tried=sorted(tried),
                note=f"Switched to {o['origin']}: {reason}",
                stuck="",
            )
            log.info("download %s: switched to %s (%s)", f["folder_name"], o["origin"], reason)
            return f"{o['origin']}|{o['folder']}"
        f["tried"] = sorted(tried)
        if (f.get("researched") or 0) < FETCH_RESEARCH:
            # Nobody in this search: ask again, other uploaders may answer now.
            text = f.get("text") or f["folder_name"]
            try:
                search_id = self.source.search(text, wait=SEARCH_WAIT)
                self.state.log_search(
                    search_id, text, f.get("artist", ""), f.get("album", "") or f["folder_name"]
                )
            except SourceError as e:
                log.info("searching again for %s: %s", f["folder_name"], e)
                f.update(stuck=reason, note="")
                return ""
            f.update(
                search_id=search_id,
                searching=reason,
                researched=(f.get("researched") or 0) + 1,
                note=f"Looking for another uploader: {reason}",
                stuck="",
            )
            return ""
        f.update(stuck=reason, note="")
        return ""

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("acquirer")
            if time.time() - self._settled > SETTLE_EVERY:
                self._settled = time.time()
                try:
                    settle(self.state, beets_ctx.lib())
                except Exception:
                    log.exception("settling requests")
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
        sid = self.source.search(text, wait=search_wait(w))
        self.state.log_search(sid, text, w["artist"], w["album"], w["id"])
        self._set(w, "searching", f"Searching for {text}", search_id=sid, error=None)
        return True

    def _give_up(self, w: dict[str, Any]) -> None:
        rounds = (w.get("researched") or 0) + 1
        tries = f"{rounds} round{'s' if rounds != 1 else ''} of searches"
        if w["offers"]:
            self._set(w, "choosing", f"No copy was clearly right after {tries}")
            n = len(w["offers"])
            copies = f"{n} cop{'ies' if n != 1 else 'y'} wait{'s' if n == 1 else ''}"
            note(self.state, w, f"Stopped after {tries}: {copies} for your pick")
        else:
            self._set(w, "failed", f"Nothing found after {tries}")
            note(self.state, w, f"Stopped after {tries}: nothing found")

    def _end_of_round(self, w: dict[str, Any], why: str) -> None:
        """Every search text was tried: another round that waits longer, or ask you."""
        n = w.get("researched") or 0
        if n >= MAX_RESEARCH:
            self._give_up(w)
            return
        self._set(
            w,
            "queued",
            f"{why[:1].upper()}{why[1:]}: searching again, waiting longer for answers",
            query_idx=0,
            search_id="",
            researched=n + 1,
            error=None,
        )
        note(self.state, w, f"{why[:1].upper()}{why[1:]}: searching again (round {n + 2})")

    def _searched(self, w: dict[str, Any]) -> None:
        info = self.source.search_info(w["search_id"])
        if info["running"]:
            return
        texts, names = _queries(w)
        text = texts[w["query_idx"]] if w["query_idx"] < len(texts) else info.get("text", "")
        tried = set(w["tried"] or [])
        # Uploaders already tried for this request let it down: skip their other folders too.
        tried_origins = {k.split("|", 1)[0] for k in tried}
        # The copies kept from this request's earlier searches compete with the new ones:
        # the best one may have been found under the release's other name.
        found = self.source.offers(w["search_id"])
        new = {f"{o['origin']}|{o['folder']}" for o in found}
        earlier = [o for o in w["offers"] or [] if f"{o['origin']}|{o['folder']}" not in new]
        picks = [
            p
            for p in rank(found + earlier, w)
            if f"{p.offer['origin']}|{p.offer['folder']}" not in tried
            and p.offer["origin"] not in tried_origins
        ]
        # Keep the best few for choosing by hand, whatever happens next.
        keep = [self._slim(p) for p in picks[:KEEP_OFFERS]]
        offers = keep or w["offers"]
        self.state.update_wanted(w["id"], offers=offers)
        w["offers"] = offers
        confident = [p for p in picks if p.confident][:MAX_LISTINGS]
        answers = info.get("answers") or 0
        self.state.search_answered(w["search_id"], answers)
        rnd = w.get("researched") or 0
        note(
            self.state,
            w,
            f"Searched for “{text}”{f' (round {rnd + 1})' if rnd else ''}: "
            f"{answers} answer{'s' if answers != 1 else ''}, "
            f"{len(picks)} folder{'s' if len(picks) != 1 else ''}"
            + (f", {len(confident)} looked right" if confident else ", none clearly right"),
        )
        if w["query_idx"] + 1 < names:
            # The release has another spelling (romanized, as written): search under it
            # too before taking a copy, so the best of both is downloaded.
            self._set(
                w,
                "queued",
                "Searching under its other name too, to take the best copy"
                if confident
                else "Trying its other name",
                query_idx=w["query_idx"] + 1,
            )
            return
        # Uploaders who answer with the folder's listing are online and share all of
        # it: try them first, then the ones who didn't answer.
        silent = []
        for p in confident:
            files = self._listing(p.offer)
            if files is None:
                silent.append(p)
            elif self._fetch(w, p.offer, files=files):
                return
        for p in silent:
            if self._fetch(w, p.offer, files=[]):
                return
        # Nothing to take unasked: the next search text, another round, or ask.
        if w["query_idx"] + 1 < len(texts):
            self._set(w, "queued", "Trying another search", query_idx=w["query_idx"] + 1)
        else:
            self._end_of_round(w, "no copy was clearly right" if w["offers"] else "nothing found")

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

    def _listing(self, offer: dict[str, Any]) -> list[dict[str, Any]] | None:
        """The folder's full listing, or None if its uploader doesn't answer."""
        try:
            return self.source.listing(offer["origin"], offer["path"]) or None
        except SourceError:
            return None

    def _fetch(
        self,
        w: dict[str, Any],
        offer: dict[str, Any],
        check: bool = True,
        files: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Download an offer's whole folder. False if its full listing rules it out.
        ``files``: its listing when already asked for ([]: the uploader didn't answer)."""
        key = f"{offer['origin']}|{offer['folder']}"
        tried = [*(w["tried"] or []), key]
        if files is None:
            files = self._listing(offer) or []
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
            progress_bytes=0,
            progress_at=time.time(),
            progress_place=None,
        )
        note(self.state, w, f"Downloading “{offer['folder_name']}” from {offer['origin']}")
        return True

    def adopt(self, w: dict[str, Any], origin: str, folder: str, folder_name: str) -> bool:
        """A copy you downloaded by hand (Find) for this request: follow it as the
        request's download, so it is imported against the release you asked for."""
        with self._lock:
            w = self.state.wanted(w["id"]) or {}
            if w.get("status") not in ADOPTABLE:
                return False
            self._set(
                w,
                "downloading",
                f"Downloading from {origin}",
                origin=origin,
                remote_folder=folder,
                folder=folder_name,
                tried=[*(w["tried"] or []), f"{origin}|{folder}"],
                attempts=w["attempts"] + 1,
                progress_bytes=0,
                progress_at=time.time(),
                progress_place=None,
                error=None,
            )
            note(self.state, w, f"You chose “{folder_name}” from {origin} under Find")
        self.wake()
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
            # Not in the last list of downloads: one that only just started may not be in
            # it yet, so the source is asked itself before the folder in the inbox (an
            # earlier copy's, say) is taken for the finished download.
            row = next(
                (
                    r
                    for r in self.source.transfers()
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
            return
        if row["state"] in ("failed", "cancelled"):
            label = row["label"][:1].lower() + row["label"][1:]
            self.abandon(w, row, f"{label} from {row['origin']}")
            return
        seen = (w["progress_bytes"] or 0, w["progress_place"])
        if moved(row, *seen) or not w["progress_at"]:
            self.state.update_wanted(
                w["id"],
                progress_bytes=row.get("transferred", 0),
                progress_place=row.get("place"),
                progress_at=time.time(),
            )
        else:
            reason = self.giving_up(row, w["progress_at"])
            if reason:
                self.abandon(w, row, reason)
                return
            if row.get("place") is not None and w["progress_place"] is None:
                # The first place seen in the queue: a mark to measure from.
                self.state.update_wanted(w["id"], progress_place=row["place"])
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

    def giving_up(self, row: dict[str, Any], since: float | None) -> str:
        """Why to stop waiting on this uploader ("" to keep waiting), when nothing moved."""
        files = row.get("files") or []
        if any(f.get("failure") == "Rejected" for f in files) and not any(
            f.get("kind") == "active" for f in files
        ):
            return f"{row['origin']} rejected it"
        if self.stall_after and since:
            idle = time.time() - since
            if idle > self.stall_after:
                minutes = round(idle / 60)
                if row["state"] == "queued":
                    return f"still queued at {row['origin']} after {minutes} minutes"
                return f"no progress from {row['origin']} for {minutes} minutes"
        return ""

    def abandon(self, w: dict[str, Any], row: dict[str, Any] | None, reason: str) -> None:
        """Stop this copy (at the uploader, and what reached the inbox), then try another."""
        if row is not None:
            ids = [f["id"] for f in row.get("files") or [] if f.get("kind") in ("queued", "active")]
            try:
                if ids:
                    self.source.cancel(row["origin"], ids, remove=True)
            except SourceError as e:
                log.info("cancelling %s for request %s: %s", row["origin"], w["id"], e)
        if w["folder"]:
            self.discard_folder(w["folder"])
        log.info("request %s: %s", w["id"], reason)
        note(self.state, w, f"Gave up on {w['origin'] or 'the copy'}: {reason}")
        self._next_offer(w, reason)

    def skip(self, w: dict[str, Any]) -> None:
        """You asked for another copy than the one downloading."""
        with self._lock:
            row = next(
                (
                    r
                    for r in self.transfers()
                    if r["origin"] == w["origin"] and r["folder"] == w["remote_folder"]
                ),
                None,
            )
            self.abandon(w, row, f"you skipped {w['origin'] or 'it'}")

    def _next_offer(self, w: dict[str, Any], reason: str) -> None:
        tried = set(w["tried"] or [])
        if w["attempts"] < MAX_ATTEMPTS:
            for o in w["offers"]:
                fresh = f"{o['origin']}|{o['folder']}" not in tried
                if o.get("confident") and fresh and self._fetch(w, o):
                    self.state.update_wanted(w["id"], error=f"Switched: {reason}")
                    return
            # Every good copy found so far was tried: look for other uploaders.
            if (w.get("researched") or 0) < MAX_RESEARCH and w["kind"] in ("missing", "upgrade"):
                n = (w.get("researched") or 0) + 1
                self._set(
                    w,
                    "queued",
                    f"{reason[:1].upper()}{reason[1:]}: searching for another uploader",
                    query_idx=0,
                    search_id="",
                    researched=n,
                    error=None,
                )
                note(self.state, w, f"Every copy found was tried: searching again (round {n + 1})")
                return
        if any(f"{o['origin']}|{o['folder']}" not in tried for o in w["offers"]):
            self._set(w, "choosing", f"The download failed ({reason}): choose another copy")
        elif w["offers"]:
            self._set(
                w, "choosing", f"Every copy found failed ({reason}): choose one, or search again"
            )
        else:
            self._set(w, "failed", f"The download failed ({reason})")

    def _arrived(self, w: dict[str, Any]) -> None:
        if self.inbox_folder_exists(w["folder"]):
            self.state.link_folder(w["folder"], w["id"])
            self._set(w, "lookup", "In the inbox, being looked up")
            note(self.state, w, f"Downloaded: “{w['folder']}” is in the inbox")
            self.on_arrival(w["folder"])
        elif time.time() - w["updated_at"] > ARRIVAL_TIMEOUT:
            self._set(
                w, "failed", f"The download finished, but no inbox folder {w['folder']} appeared"
            )


# --------------------------------------------------------------- settling

# Requests waiting on you, or not started, that are over once the album is in
# the library however it got there: another copy, the console, a re-import.
# Requests mid-search, mid-download or importing finish on their own.
SETTLE = ("queued", "choosing", "review", "failed")
SETTLE_EVERY = 300  # seconds between checks from the request loop


def settle(state: State, lib: Any) -> int:
    """Mark requests whose album is already in the library as done. Returns how many."""
    open_ = [w for w in state.wanted_list(SETTLE) if w["kind"] == "missing"]
    if not open_:
        return 0
    index = discography.owned_index(lib)
    n = 0
    for w in open_:
        if discography.is_owned(
            index,
            [w["release_id"], w["lookup_id"]],
            [w["artist"], w["artist_original"] or ""],
            [w["album"], w["album_original"] or ""],
        ):
            state.update_wanted(w["id"], status="done", status_text="In your library", error=None)
            note(state, w, "Found in your library")
            n += 1
    return n


# ------------------------------------------------------- downloads from Find

FETCHES_KEY = "fetches"  # downloads started from Find, by "origin|remote folder"
FETCH_KEEP = 7 * 86400
FETCH_RESEARCH = 1  # searches started again for a download from Find nobody else has


def moved(row: dict[str, Any], seen_bytes: int, seen_place: int | None) -> bool:
    """Did a download progress since the last look: more bytes, or up the uploader's queue?"""
    if row.get("transferred", 0) > (seen_bytes or 0):
        return True
    place = row.get("place")
    return place is not None and seen_place is not None and place < seen_place


def file_name(filename: str) -> str:
    """The last part of a remote path (Soulseek paths use backslashes)."""
    return filename.replace("\\", "/").rsplit("/", 1)[-1]


def remember_fetch(
    state: State,
    origin: str,
    folder: str,
    folder_name: str,
    search_id: str,
    text: str,
    tracks: int,
    artist: str = "",
    album: str = "",
) -> None:
    """A download started from Find: enough to find the same folder elsewhere if it
    stalls, and the release it is for when the search knew it."""
    fetches = state.meta(FETCHES_KEY) or {}
    f = new_fetch(folder_name, search_id, text, tracks)
    if album:
        f.update(artist=artist, album=album)
    fetches[f"{origin}|{folder}"] = f
    state.set_meta(FETCHES_KEY, fetches)


def new_fetch(folder_name: str, search_id: str, text: str, tracks: int) -> dict[str, Any]:
    """What is kept about a download from Find (``tracks`` 0: unknown)."""
    now = time.time()
    return {
        "folder_name": folder_name,
        "search_id": search_id,
        "text": text,
        "tracks": tracks,
        "at": now,
        "progress_bytes": 0,
        "progress_at": now,
        "tried": [],
    }


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
        note(state, w, text)


def inbox_gone(state: State, w: dict[str, Any]) -> None:
    """A requested folder left the inbox before it was imported (deleted there, or by
    hand): the request stops, where you can search again, find it by hand or remove it,
    instead of waiting for a review that can no longer happen."""
    text = "Its folder was deleted from the inbox"
    state.update_wanted(w["id"], status="failed", status_text=text, folder="", error=None)
    note(state, w, f"{text} before it was imported")


def inbox_imported(state: State, w: dict[str, Any], ok: bool, why: str = "") -> None:
    if ok:
        state.update_wanted(w["id"], status="done", status_text="In your library", error=None)
        note(state, w, "Imported into your library")
    else:
        state.update_wanted(w["id"], status="review", status_text=f"In the inbox: {why}")
        note(state, w, f"Waiting for review in the inbox: {why}")

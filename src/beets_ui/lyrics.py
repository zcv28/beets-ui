"""Synced lyrics from LRCLIB, with romaji under Japanese lines.

Lyrics are looked up on LRCLIB (https://lrclib.net) by the names as
originally written first (``title_original`` etc., from romanizing), since
LRCLIB lists Japanese songs under their Japanese names, then by the main
tags. Only time-synced lyrics are used.

For Japanese songs each line gets a romaji line, from:

* VocaDB/TouhouDB/UtaiteDB's own romanized lyrics, for tracks tagged from
  there, when they have exactly as many lines as the synced lyrics;
* otherwise cutlet (MeCab with the UniDic dictionary), line by line.

``BEETSUI_LYRICS_ROMAJI`` sets the layout: ``below`` (each romaji line
right after its line, with the same time), ``inline`` (``line / romaji``,
the way beets stores translations), ``only`` (romaji instead of the
Japanese) or ``off``. The result is stored in the track's ``lyrics`` field,
with beets' own ``lyrics_backend``/``lyrics_url`` fields, and written into
the file, where Jellyfin picks it up.
"""

from __future__ import annotations

import difflib
import logging
import re
import time
import unicodedata
from typing import Any

import httpx

from . import __version__, beets_ctx
from .history import Snapshot
from .jobs import JobContext, JobQueue
from .romanize import VOCADB_SOURCES
from .settings import Settings

log = logging.getLogger("beets_ui.lyrics")

LRCLIB = "https://lrclib.net/api"
USER_AGENT = f"beets-ui/{__version__} (+https://github.com/zcv28/beets-ui)"
VOCADB_API = {
    "vocadb": "https://vocadb.net/api",
    "touhoudb": "https://touhoudb.com/api",
    "utaitedb": "https://utaitedb.net/api",
}
STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
KANA = re.compile(r"[぀-ヿｦ-ﾟ]")
CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")
RECHECK_AFTER = 30 * 86400
# beets' own lyrics metadata fields, plus which romaji was added.
META = (
    "lyrics_backend",
    "lyrics_url",
    "lyrics_romaji",
    "lyrics_translation_language",
    "lyrics_instrumental",
)
BULK_LIMIT = 300


class LyricsError(RuntimeError):
    pass


# ------------------------------------------------------------------- LRC


def is_synced(text: str | None) -> bool:
    return bool(text) and any(STAMP.match(line.strip()) for line in text.splitlines())


def parse_lrc(text: str) -> list[tuple[int, str]]:
    """(milliseconds, text) per timed line, in time order; tags like [ar:] are dropped."""
    out: list[tuple[int, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        stamps = []
        while m := STAMP.match(line):
            mins, secs = m.group(1), m.group(2).replace(":", ".")
            stamps.append(round((int(mins) * 60 + float(secs)) * 1000))
            line = line[m.end() :]
        for ms in stamps:
            out.append((ms, line.strip()))
    out.sort(key=lambda x: x[0])
    return out


def stamp(ms: int) -> str:
    cs = round(ms / 10)
    return f"[{cs // 6000:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}]"


def grouped(text: str) -> list[dict[str, Any]]:
    """Lines for display: lines sharing a time are a line and its romaji."""
    rows: list[dict[str, Any]] = []
    for ms, line in parse_lrc(text):
        if rows and rows[-1]["ms"] == ms:
            rows[-1]["sub"].append(line)
        else:
            rows.append({"ms": ms, "time": stamp(ms)[1:-1], "text": line, "sub": []})
    return rows


def is_japanese(lines: list[str]) -> bool:
    return any(KANA.search(line) for line in lines)


# ---------------------------------------------------------------- romaji


class Romanizer:
    """Japanese to romaji with cutlet, loaded on first use."""

    def __init__(self):
        self._cutlet: Any = None

    def __call__(self, line: str) -> str:
        if not CJK.search(line):
            return line
        if self._cutlet is None:
            try:
                import cutlet
            except ImportError as e:  # pragma: no cover - installed with beets-ui
                raise LyricsError("romaji needs the cutlet package") from e
            self._cutlet = cutlet.Cutlet()
            self._cutlet.use_foreign_spelling = False
        return self._cutlet.romaji(unicodedata.normalize("NFKC", line)).strip()


def vocadb_romaji(http: httpx.Client, source: str, song_id: str) -> list[str] | None:
    """The song's romanized lyrics from VocaDB & co, as non-empty lines."""
    api = VOCADB_API.get(source)
    if not api or not str(song_id).isdigit():
        return None
    r = http.get(f"{api}/songs/{song_id}", params={"fields": "Lyrics"})
    if r.status_code >= 400:
        return None
    for lyr in r.json().get("lyrics") or []:
        if lyr.get("translationType") == "Romanized" and lyr.get("value"):
            return [line.strip() for line in lyr["value"].splitlines() if line.strip()]
    return None


def combine(
    lines: list[tuple[int, str]], romaji: list[str | None], mode: str
) -> str:  # one LRC text
    out = []
    for (ms, text), rom in zip(lines, romaji, strict=True):
        if mode == "only" and rom:
            out.append(f"{stamp(ms)}{rom}")
            continue
        if mode == "inline" and rom and rom != text:
            out.append(f"{stamp(ms)}{text} / {rom}")
            continue
        out.append(f"{stamp(ms)}{text}")
        if mode == "below" and rom and rom != text:
            out.append(f"{stamp(ms)}{rom}")
    return "\n".join(out) + "\n"


def add_romaji(
    lrc: str, mode: str, romanizer: Romanizer, vocadb_lines: list[str] | None = None
) -> tuple[str, str]:
    """The LRC text in the configured layout, and where its romaji came from."""
    lines = parse_lrc(lrc)
    texts = [t for _, t in lines]
    if mode == "off" or not is_japanese(texts):
        return combine(lines, [None] * len(lines), "off"), ""
    sung = [i for i, t in enumerate(texts) if t]
    romaji: list[str | None] = [None] * len(lines)
    if vocadb_lines and len(vocadb_lines) == len(sung):
        for i, rom in zip(sung, vocadb_lines, strict=True):
            romaji[i] = rom
        return combine(lines, romaji, mode), "vocadb"
    generated = [romanizer(texts[i]) for i in sung]
    picked = align(generated, vocadb_lines or [])
    for i, gen, rom in zip(sung, generated, picked, strict=True):
        romaji[i] = rom or gen
    used = sum(1 for rom in picked if rom)
    how = "cutlet" if not used else "vocadb" if used == len(sung) else "vocadb+cutlet"
    return combine(lines, romaji, mode), how


def _key(romaji: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", romaji).lower())


def align(generated: list[str], human: list[str], look: int = 4) -> list[str | None]:
    """For each generated line, the human romaji line that says the same, if any.

    Both lists run in song order, so this walks forward through ``human``,
    looking a few lines ahead for one that is close enough to the generated
    romaji (lyrics sites often add or merge a line or two).
    """
    out: list[str | None] = []
    j = 0
    for gen in generated:
        g = _key(gen)
        best, best_at = 0.0, -1
        for k in range(j, min(j + look, len(human))):
            ratio = difflib.SequenceMatcher(None, g, _key(human[k]), autojunk=False).ratio()
            if ratio > best:
                best, best_at = ratio, k
        if g and best >= 0.6:
            out.append(human[best_at])
            j = best_at + 1
        else:
            out.append(None)
    return out


# ---------------------------------------------------------------- LRCLIB


def _queries(item: Any) -> list[tuple[str, str, str]]:
    out = []
    for title, artist, album in (
        (item.get("title_original"), item.get("artist_original"), item.get("album_original")),
        (item.title, item.artist, item.album),
    ):
        q = (title or item.title, artist or item.artist, album or item.album)
        if q[0] and q[1] and q not in out:
            out.append(q)
    return out


def _close(hit: dict, length: float) -> bool:
    return bool(hit.get("syncedLyrics")) and abs(float(hit.get("duration") or 0) - length) <= 3


def lrclib_find(http: httpx.Client, item: Any) -> dict | None:
    """The best LRCLIB entry with synced lyrics (or an instrumental marker)."""
    length = float(item.length or 0)
    for title, artist, album in _queries(item):
        params = {"track_name": title, "artist_name": artist, "album_name": album}
        if length:
            params["duration"] = str(round(length))
        r = http.get(f"{LRCLIB}/get", params=params)
        if r.status_code == 200:
            hit = r.json()
            if hit.get("instrumental") or hit.get("syncedLyrics"):
                return hit
        elif r.status_code != 404:
            r.raise_for_status()
    for title, artist, _album in _queries(item):
        r = http.get(f"{LRCLIB}/search", params={"track_name": title, "artist_name": artist})
        if r.status_code != 200:
            continue
        hits = [h for h in r.json() if _close(h, length)] if length else []
        if hits:
            return hits[0]
    return None


def http_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=20,
        follow_redirects=True,
    )


# ------------------------------------------------------------------- job


def _wanted(lyrics: str | None, flex: dict[str, str], p: dict, bulk: bool, now: float) -> bool:
    if p.get("force"):
        return True
    if is_synced(lyrics) or flex.get("lyrics_instrumental") not in (None, "", "0", "False"):
        return False
    if not p.get("replace_plain", True) and lyrics:
        return False
    try:
        checked = float(flex.get("lyrics_checked") or 0)
    except ValueError:
        checked = 0
    return not (bulk and now - checked < RECHECK_AFTER)


def _candidates(lib: Any, p: dict, bulk: bool, now: float) -> list[int]:
    """IDs of the tracks to look up, read with plain SQL (no Item objects)."""
    where, args = "", []
    if p.get("item_ids"):
        ids = [int(x) for x in p["item_ids"]]
        where, args = f"WHERE id IN ({','.join('?' * len(ids))})", ids
    elif p.get("album_ids"):
        ids = [int(x) for x in p["album_ids"]]
        where, args = f"WHERE album_id IN ({','.join('?' * len(ids))})", ids
    with lib.transaction() as tx:
        rows = tx.query(f"SELECT id, lyrics FROM items {where} ORDER BY id", args)
        flex: dict[int, dict[str, str]] = {}
        for entity, key, value in tx.query(
            "SELECT entity_id, key, value FROM item_attributes "
            "WHERE key IN ('lyrics_checked', 'lyrics_instrumental')"
        ):
            flex.setdefault(entity, {})[key] = value
    return [i for i, text in rows if _wanted(text, flex.get(i, {}), p, bulk, now)]


def coverage(lib: Any) -> dict[str, int]:
    with lib.transaction() as tx:
        rows = tx.query("SELECT lyrics FROM items")
        instrumental = tx.query(
            "SELECT COUNT(DISTINCT entity_id) FROM item_attributes "
            "WHERE key='lyrics_instrumental' AND value NOT IN ('', '0', 'False')"
        )[0][0]
    synced = sum(1 for (t,) in rows if is_synced(t))
    plain = sum(1 for (t,) in rows if t and not is_synced(t))
    return {"tracks": len(rows), "synced": synced, "plain": plain, "instrumental": instrumental}


def register_jobs(jobs: JobQueue, settings: Settings, http_factory=http_client) -> None:
    @jobs.register("lyrics")
    def lyrics(ctx: JobContext) -> None:
        p = ctx.params
        # Runs from the Lyrics page skip tracks not found lately and stop after
        # ``limit`` tracks, so the job queue isn't blocked for hours.
        bulk = bool(p.get("bulk")) or not (p.get("item_ids") or p.get("album_ids"))
        limit = int(p.get("limit") or BULK_LIMIT)
        mode = p.get("mode") or settings.lyrics_romaji
        lib = beets_ctx.lib()
        http = http_factory()
        romanizer = Romanizer()
        snap = Snapshot(ctx, "Synced lyrics")
        now = time.time()
        ids = _candidates(lib, p, bulk, now)
        left = len(ids) - limit if limit and len(ids) > limit else 0
        if left:
            ids = ids[:limit]
        todo = [i for i in (lib.get_item(x) for x in ids) if i is not None]
        ctx.log(f"Looking up synced lyrics for {len(todo)} track(s)")
        found = 0
        for item in todo:
            label = f"{item.artist} - {item.title}"
            try:
                hit = lrclib_find(http, item)
            except httpx.HTTPError as e:
                ctx.log(f"  {label}: LRCLIB error {e}")
                continue
            if hit is None:
                ctx.log(f"  {label}: not found")
                item["lyrics_checked"] = int(now)
                item.store()
                continue
            if hit.get("instrumental"):
                ctx.log(f"  {label}: instrumental")
                item["lyrics_instrumental"] = True
                item["lyrics_checked"] = int(now)
                item.store()
                continue
            vocadb_lines = None
            src = str(item.get("data_source") or "").lower()
            if mode != "off" and src in VOCADB_SOURCES and item.mb_trackid:
                try:
                    vocadb_lines = vocadb_romaji(http, src, item.mb_trackid)
                except (httpx.HTTPError, ValueError):
                    vocadb_lines = None
            text, how = add_romaji(hit["syncedLyrics"], mode, romanizer, vocadb_lines)
            snap.capture(item, ["lyrics", *META, "lyrics_checked"])
            item.lyrics = text
            item["lyrics_backend"] = "lrclib"
            item["lyrics_url"] = f"https://lrclib.net/api/get/{hit['id']}" if hit.get("id") else ""
            item["lyrics_romaji"] = how
            item["lyrics_checked"] = int(now)
            for k in ("lyrics_translation_language", "lyrics_instrumental"):
                if k in item:
                    del item[k]
            item.try_sync(write=True, move=False)
            found += 1
            ctx.log(f"  {label}: synced lyrics" + (f", romaji from {how}" if how else ""))
        snap.save(write=True, move=False)
        ctx.log(f"Done: synced lyrics for {found} of {len(todo)} track(s)")
        if left:
            ctx.log(f"{left} more track(s) to look up: run it again to continue")

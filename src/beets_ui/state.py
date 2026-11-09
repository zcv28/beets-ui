"""Small SQLite store for beets-ui's own state (inbox folders and jobs).

This is separate from the beets library database.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    path TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'new',
    signature TEXT NOT NULL DEFAULT '',
    changed_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    best REAL,
    mode TEXT NOT NULL DEFAULT 'albums',
    error TEXT,
    tasks TEXT NOT NULL DEFAULT '[]',
    duplicates TEXT NOT NULL DEFAULT '{}',
    decisions TEXT NOT NULL DEFAULT '{}',
    job_id INTEGER
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    params TEXT NOT NULL DEFAULT '{}',
    log TEXT NOT NULL DEFAULT '',
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    user TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS artist_art (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS artist_aliases (
    alias TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    label TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at REAL NOT NULL,
    undone_by INTEGER
);
CREATE INDEX IF NOT EXISTS snapshots_job ON snapshots(job_id);
CREATE TABLE IF NOT EXISTS artist_romaji (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS discography (
    source TEXT NOT NULL,
    artist_id TEXT NOT NULL,
    artist TEXT NOT NULL,
    releases TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (source, artist_id)
);
CREATE TABLE IF NOT EXISTS dismissed (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS plays (
    item_id INTEGER PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0,
    last_played REAL,
    favorite INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns added after the first release: (name, definition).
FOLDER_MIGRATIONS = (
    ("files", "TEXT NOT NULL DEFAULT '[]'"),
    ("tagkeys", "TEXT NOT NULL DEFAULT '{}'"),
    ("library", "TEXT NOT NULL DEFAULT '[]'"),
)

FOLDER_JSON = ("tasks", "duplicates", "decisions", "files", "tagkeys", "library")
FOLDER_JSON_LISTS = ("tasks", "files", "library")


class State:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(folders)")}
            for name, definition in FOLDER_MIGRATIONS:
                if name not in cols:
                    self._conn.execute(f"ALTER TABLE folders ADD COLUMN {name} {definition}")
            # Jobs interrupted by a restart can never finish.
            self._conn.execute(
                "UPDATE jobs SET status='failed', error='interrupted by restart', "
                "finished_at=? WHERE status IN ('queued','running')",
                (time.time(),),
            )
            self._conn.execute(
                "UPDATE folders SET status='new', job_id=NULL "
                "WHERE status IN ('queued','previewing','importing')"
            )

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, args)

    # ----------------------------------------------------------------- folders

    @staticmethod
    def _folder(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        for k in FOLDER_JSON:
            d[k] = json.loads(d[k] or ("[]" if k in FOLDER_JSON_LISTS else "{}"))
        return d

    def folders(self) -> list[dict[str, Any]]:
        rows = self._exec("SELECT * FROM folders ORDER BY path COLLATE NOCASE").fetchall()
        return [self._folder(r) for r in rows]  # type: ignore[misc]

    def folder(self, path: str) -> dict[str, Any] | None:
        return self._folder(self._exec("SELECT * FROM folders WHERE path=?", (path,)).fetchone())

    def upsert_folder(self, path: str, **fields: Any) -> None:
        fields = {k: (json.dumps(v) if k in FOLDER_JSON else v) for k, v in fields.items()}
        fields["updated_at"] = time.time()
        with self._lock:
            exists = self._conn.execute("SELECT 1 FROM folders WHERE path=?", (path,)).fetchone()
            if exists:
                sets = ", ".join(f"{k}=?" for k in fields)
                self._conn.execute(
                    f"UPDATE folders SET {sets} WHERE path=?", (*fields.values(), path)
                )
            else:
                cols = ", ".join(["path", *fields])
                qs = ", ".join("?" * (len(fields) + 1))
                self._conn.execute(
                    f"INSERT INTO folders ({cols}) VALUES ({qs})", (path, *fields.values())
                )

    def delete_folder(self, path: str) -> None:
        self._exec("DELETE FROM folders WHERE path=?", (path,))

    # -------------------------------------------------------------------- jobs

    def create_job(self, kind: str, title: str, params: dict, user: str | None = None) -> int:
        cur = self._exec(
            "INSERT INTO jobs (kind, title, params, created_at, user) VALUES (?,?,?,?,?)",
            (kind, title, json.dumps(params), time.time(), user),
        )
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def job(self, job_id: int) -> dict[str, Any] | None:
        row = self._exec("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["params"] = json.loads(d["params"])
        return d

    def jobs(self, limit: int = 100, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            rows = self._exec(
                "SELECT id, kind, title, status, error, created_at, started_at, finished_at, user "
                "FROM jobs WHERE kind=? ORDER BY id DESC LIMIT ?",
                (kind, limit),
            )
        else:
            rows = self._exec(
                "SELECT id, kind, title, status, error, created_at, started_at, finished_at, user "
                "FROM jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        return [dict(r) for r in rows.fetchall()]

    def jobs_finished_since(self, ts: float) -> list[dict[str, Any]]:
        rows = self._exec(
            "SELECT id, kind, title, status, error, finished_at FROM jobs "
            "WHERE finished_at > ? ORDER BY finished_at LIMIT 20",
            (ts,),
        )
        return [dict(r) for r in rows.fetchall()]

    def set_job(self, job_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))

    def append_log(self, job_id: int, text: str) -> None:
        self._exec("UPDATE jobs SET log = log || ? WHERE id=?", (text, job_id))

    def prune_jobs(self, keep: int = 500) -> None:
        self._exec(
            "DELETE FROM jobs WHERE id NOT IN (SELECT id FROM jobs ORDER BY id DESC LIMIT ?) "
            "AND status NOT IN ('queued','running')",
            (keep,),
        )
        self._exec("DELETE FROM snapshots WHERE job_id NOT IN (SELECT id FROM jobs)")

    # -------------------------------------------------------------- artist art

    def artist_art(self) -> dict[str, dict[str, Any]]:
        rows = self._exec("SELECT * FROM artist_art").fetchall()
        return {r["name"]: dict(r) for r in rows}

    def set_artist_art(
        self, name: str, status: str, source: str = "", url: str = "", note: str = ""
    ) -> None:
        self._exec(
            "INSERT INTO artist_art (name, status, source, url, note, updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET status=excluded.status, "
            "source=excluded.source, url=excluded.url, note=excluded.note, "
            "updated_at=excluded.updated_at",
            (name, status, source, url, note[:500], time.time()),
        )

    # ---------------------------------------------------------- artist aliases

    def artist_aliases(self) -> dict[str, str]:
        """Casefolded alias -> the album artist name it is merged into."""
        rows = self._exec("SELECT alias, name FROM artist_aliases").fetchall()
        return {r["alias"]: r["name"] for r in rows}

    def set_artist_alias(self, alias: str, name: str) -> None:
        key = alias.strip().casefold()
        if not key or key == name.strip().casefold():
            return
        with self._lock:
            # Aliases that pointed at ``alias`` now point at its new name.
            self._conn.execute("UPDATE artist_aliases SET name=? WHERE name=?", (name, alias))
            self._conn.execute(
                "INSERT INTO artist_aliases (alias, name, created_at) VALUES (?,?,?) "
                "ON CONFLICT(alias) DO UPDATE SET name=excluded.name",
                (key, name, time.time()),
            )
            self._conn.execute(
                "DELETE FROM artist_aliases WHERE alias=?", (name.strip().casefold(),)
            )

    def delete_artist_alias(self, alias: str) -> None:
        self._exec("DELETE FROM artist_aliases WHERE alias=?", (alias.casefold(),))

    # ----------------------------------------------------------- artist romaji

    def artist_romaji(self, key: str) -> tuple[str, float] | None:
        """Cached romanized artist name ("" = none found) and when it was looked up."""
        row = self._exec(
            "SELECT name, updated_at FROM artist_romaji WHERE key=?", (key,)
        ).fetchone()
        return (row["name"], row["updated_at"]) if row else None

    def set_artist_romaji(self, key: str, name: str) -> None:
        self._exec(
            "INSERT INTO artist_romaji (key, name, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
            (key, name, time.time()),
        )

    # --------------------------------------------------------------- snapshots

    def add_snapshot(self, job_id: int, label: str, data: dict[str, Any]) -> int:
        cur = self._exec(
            "INSERT INTO snapshots (job_id, label, data, created_at) VALUES (?,?,?,?)",
            (job_id, label, json.dumps(data), time.time()),
        )
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def snapshot(self, snap_id: int) -> dict[str, Any] | None:
        row = self._exec("SELECT * FROM snapshots WHERE id=?", (snap_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["data"] = json.loads(d["data"])
        return d

    def snapshot_for_job(self, job_id: int) -> dict[str, Any] | None:
        row = self._exec(
            "SELECT id FROM snapshots WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)
        ).fetchone()
        return self.snapshot(row["id"]) if row else None

    def mark_snapshot_undone(self, snap_id: int, job_id: int) -> None:
        self._exec("UPDATE snapshots SET undone_by=? WHERE id=?", (job_id, snap_id))

    # ------------------------------------------------------------ discography

    def discographies(self) -> list[dict[str, Any]]:
        rows = self._exec("SELECT * FROM discography").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["releases"] = json.loads(d["releases"] or "[]")
            out.append(d)
        return out

    def set_discography(
        self, source: str, artist_id: str, artist: str, releases: list[dict], error: str = ""
    ) -> None:
        with self._lock:
            if error:
                # Keep the releases from the last good lookup.
                self._conn.execute(
                    "INSERT INTO discography (source, artist_id, artist, error, updated_at) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(source, artist_id) DO UPDATE SET "
                    "artist=excluded.artist, error=excluded.error, updated_at=excluded.updated_at",
                    (source, artist_id, artist, error[:500], time.time()),
                )
                return
            self._conn.execute(
                "INSERT INTO discography (source, artist_id, artist, releases, error, updated_at) "
                "VALUES (?,?,?,?,NULL,?) ON CONFLICT(source, artist_id) DO UPDATE SET "
                "artist=excluded.artist, releases=excluded.releases, error=NULL, "
                "updated_at=excluded.updated_at",
                (source, artist_id, artist, json.dumps(releases), time.time()),
            )

    def prune_discography(self, keep: set[tuple[str, str]]) -> None:
        """Forget artists that are no longer in the library."""
        with self._lock:
            for r in self._conn.execute("SELECT source, artist_id FROM discography").fetchall():
                if (r["source"], r["artist_id"]) not in keep:
                    self._conn.execute(
                        "DELETE FROM discography WHERE source=? AND artist_id=?",
                        (r["source"], r["artist_id"]),
                    )

    # -------------------------------------------------------------- dismissed

    def dismissed(self, kind: str) -> dict[str, str]:
        rows = self._exec("SELECT key, label FROM dismissed WHERE kind=?", (kind,)).fetchall()
        return {r["key"]: r["label"] for r in rows}

    def dismiss(self, kind: str, key: str, label: str = "") -> None:
        self._exec(
            "INSERT INTO dismissed (kind, key, label, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(kind, key) DO UPDATE SET label=excluded.label",
            (kind, key, label[:300], time.time()),
        )

    def undismiss(self, kind: str, key: str) -> None:
        self._exec("DELETE FROM dismissed WHERE kind=? AND key=?", (kind, key))

    # ------------------------------------------------------------------ plays

    def plays(self) -> dict[int, dict[str, Any]]:
        rows = self._exec("SELECT * FROM plays").fetchall()
        return {r["item_id"]: dict(r) for r in rows}

    def replace_plays(self, rows: list[tuple[int, int, float | None, bool]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM plays")
                self._conn.executemany(
                    "INSERT OR REPLACE INTO plays (item_id, count, last_played, favorite) "
                    "VALUES (?,?,?,?)",
                    [(i, c, last, int(fav)) for i, c, last, fav in rows],
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------- meta

    def meta(self, key: str, default: Any = None) -> Any:
        row = self._exec("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

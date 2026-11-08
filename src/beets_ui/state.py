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
"""

FOLDER_JSON = ("tasks", "duplicates", "decisions")


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
            d[k] = json.loads(d[k] or ("[]" if k == "tasks" else "{}"))
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

"""slskd REST client: searches, full folder listings, downloads, server state.

slskd (https://github.com/slskd/slskd) does the Soulseek side; beets-ui drives it through
its API and receives the finished downloads in the inbox, which is slskd's downloads folder.
"""

from __future__ import annotations

import posixpath
import uuid
from collections import defaultdict
from typing import Any
from urllib.parse import quote

import httpx

from .sources.base import SourceError

AUDIO_EXT = {
    "mp3",
    "flac",
    "m4a",
    "aac",
    "ogg",
    "opus",
    "wav",
    "wv",
    "ape",
    "aiff",
    "aif",
    "alac",
    "wma",
    "mpc",
    "dsf",
    "dff",
}
LOSSLESS_EXT = {"flac", "wav", "alac", "ape", "wv", "aiff", "aif", "dsf", "dff"}

# Search options as offered in the UI. slskd's own defaults: 15 s, any queue, any speed.
WAIT_CHOICES = (15, 30, 60, 120)
QUEUE_CHOICES = (0, 10, 50, 200)  # 0: any
SPEED_CHOICES = (0, 100_000, 500_000, 1_000_000, 5_000_000)  # bytes per second; 0: any
SEARCH_DEFAULTS = {"wait": 15, "max_queue": 0, "min_speed": 0}


class SlskdError(SourceError):
    pass


def _split(filename: str) -> tuple[str, str]:
    """Soulseek paths use backslashes; return (directory, basename) with forward slashes."""
    norm = filename.replace("\\", "/")
    return posixpath.dirname(norm), posixpath.basename(norm)


def _raw_dir(filename: str) -> str:
    """The directory part of a Soulseek path, separators as the peer sent them."""
    cut = max(filename.rfind("\\"), filename.rfind("/"))
    return filename[:cut] if cut > 0 else ""


def _ext(f: dict[str, Any], name: str) -> str:
    return (f.get("extension") or posixpath.splitext(name)[1].lstrip(".")).lower()


def _file(f: dict[str, Any], filename: str) -> dict[str, Any]:
    name = _split(filename)[1]
    ext = _ext(f, name)
    return {
        "filename": filename,
        "name": name,
        "size": f.get("size", 0) or 0,
        "bitrate": f.get("bitRate"),
        "length": f.get("length"),
        "ext": ext,
        "audio": ext in AUDIO_EXT,
    }


def _user(username: str) -> str:
    return quote(username, safe="")


def summarise(files: list[dict[str, Any]]) -> dict[str, Any]:
    """Format, average bitrate, losslessness and size of a folder's files."""
    exts: dict[str, int] = defaultdict(int)
    rates = []
    for f in files:
        if f["audio"]:
            exts[f["ext"]] += 1
            if f.get("bitrate"):
                rates.append(f["bitrate"])
    return {
        "format": "/".join(sorted(exts, key=lambda e: -exts[e])).upper(),
        "bitrate": round(sum(rates) / len(rates)) if rates else None,
        "lossless": any(e in LOSSLESS_EXT for e in exts),
        "size": sum(f["size"] for f in files),
        "tracks": sum(exts.values()),
    }


class Slskd:
    def __init__(self, url: str, api_key: str, transport: httpx.BaseTransport | None = None):
        self.client = httpx.Client(
            base_url=url.rstrip("/") + "/api/v0",
            headers={"X-API-Key": api_key},
            timeout=15,
            transport=transport,
        )

    def _req(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self.client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise SlskdError(f"Can't reach slskd: {e}") from e
        if r.status_code >= 400:
            raise SlskdError(f"slskd {method} {path}: HTTP {r.status_code} {r.text[:200]}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    # ---------------------------------------------------------------- server

    def server(self) -> dict[str, Any]:
        return self._req("GET", "/server") or {}

    def connect(self) -> None:
        self._req("PUT", "/server")

    # ---------------------------------------------------------------- search

    def search(self, text: str, wait: int = 15, max_queue: int = 0, min_speed: int = 0) -> str:
        search_id = str(uuid.uuid4())
        body: dict[str, Any] = {"id": search_id, "searchText": text, "searchTimeout": wait * 1000}
        if max_queue:
            body["maximumPeerQueueLength"] = max_queue
        if min_speed:
            body["minimumPeerUploadSpeed"] = min_speed
        self._req("POST", "/searches", json=body)
        return search_id

    def search_state(self, search_id: str) -> dict[str, Any]:
        return self._req("GET", f"/searches/{search_id}")

    def search_responses(self, search_id: str) -> list[dict[str, Any]]:
        return self._req("GET", f"/searches/{search_id}/responses") or []

    def searches(self) -> list[dict[str, Any]]:
        return self._req("GET", "/searches") or []

    def stop_search(self, search_id: str) -> None:
        self._req("PUT", f"/searches/{search_id}")

    def delete_search(self, search_id: str) -> None:
        self._req("DELETE", f"/searches/{search_id}")

    # ----------------------------------------------------------------- users

    def directory(self, username: str, path: str) -> list[dict[str, Any]]:
        """Every file in one of the user's folders, asked of the user directly.

        A search only returns the files that matched; this is the whole folder.
        """
        data = self._req("POST", f"/users/{_user(username)}/directory", json={"directory": path})
        # slskd answers with one directory or a list of them, depending on the version.
        dirs = (data.get("directories") or [data]) if isinstance(data, dict) else (data or [])
        want = path.replace("\\", "/").rstrip("/")
        found = next(
            (d for d in dirs if str(d.get("name", "")).replace("\\", "/").rstrip("/") == want),
            dirs[0] if dirs else None,
        )
        if found is None:
            raise SlskdError(f"{username} sent no folder listing")
        base = found.get("name") or path
        sep = "/" if "/" in base and "\\" not in base else "\\"
        out = []
        for f in found.get("files", []) or []:
            name = f.get("filename", "")
            # Browse listings name files without their folder; search results with it.
            full = name if ("\\" in name or "/" in name) else f"{base}{sep}{name}"
            out.append(_file(f, full))
        out.sort(key=lambda f: f["name"].lower())
        return out

    # ------------------------------------------------------------- transfers

    def enqueue(self, username: str, files: list[dict[str, Any]]) -> None:
        body = [{"filename": f["filename"], "size": f["size"]} for f in files]
        self._req("POST", f"/transfers/downloads/{_user(username)}", json=body)

    def downloads(self) -> list[dict[str, Any]]:
        return self._req("GET", "/transfers/downloads") or []

    def cancel(self, username: str, transfer_id: str, remove: bool = False) -> None:
        self._req(
            "DELETE",
            f"/transfers/downloads/{_user(username)}/{transfer_id}",
            params={"remove": str(remove).lower()},
        )

    def clear_completed(self) -> None:
        self._req("DELETE", "/transfers/downloads/all/completed")


def group_responses(responses: list[dict[str, Any]], audio_only: bool = True) -> list[dict]:
    """Group search results by (user, folder); one row per downloadable folder."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for resp in responses:
        user = resp.get("username", "")
        for f in resp.get("files", []) or []:
            filename = f.get("filename", "")
            folder, _ = _split(filename)
            file = _file(f, filename)
            if audio_only and not file["audio"]:
                continue
            g = groups.setdefault(
                (user, folder),
                {
                    "username": user,
                    "origin": user,
                    "folder": folder,
                    "path": _raw_dir(filename),
                    "folder_name": posixpath.basename(folder) or folder,
                    "files": [],
                    "free_slot": bool(resp.get("hasFreeUploadSlot")),
                    "queue": resp.get("queueLength", 0),
                    "speed": resp.get("uploadSpeed", 0),
                    "locked": False,
                },
            )
            g["files"].append(file)
        for f in resp.get("lockedFiles", []) or []:
            folder, _ = _split(f.get("filename", ""))
            if (user, folder) in groups:
                groups[(user, folder)]["locked"] = True
    out = []
    for g in groups.values():
        g["files"].sort(key=lambda f: f["name"].lower())
        g.update(summarise(g["files"]))
        out.append(g)
    out.sort(key=lambda g: (not g["free_slot"], not g["lossless"], g["queue"], -g["speed"]))
    return out


# slskd transfer states read "Completed, Succeeded", "Completed, Errored", "InProgress",
# "Queued, Remotely" and so on.
FAILURES = {
    "Errored": "Error",
    "Rejected": "Rejected",
    "TimedOut": "Timed out",
    "Cancelled": "Cancelled",
    "Aborted": "Aborted",
    "Failed": "Failed",
}


def file_kind(state: str) -> str:
    if "Succeeded" in state:
        return "done"
    if "Completed" in state:
        return "failed"
    if "InProgress" in state or "Initializing" in state:
        return "active"
    return "queued"


def failure_label(state: str) -> str:
    return next((label for key, label in FAILURES.items() if key in state), "Failed")


def flatten_downloads(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group slskd transfers into one row per (user, remote folder)."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for user in data or []:
        username = user.get("username", "")
        for d in user.get("directories", []) or []:
            folder = d.get("directory", "")
            row = rows.setdefault(
                (username, folder),
                {
                    "username": username,
                    "origin": username,
                    "folder": folder,
                    "folder_name": posixpath.basename(folder.replace("\\", "/")),
                    "files": [],
                    "size": 0,
                    "transferred": 0,
                },
            )
            for f in d.get("files", []) or []:
                state = f.get("state", "") or ""
                row["files"].append(
                    {
                        "id": f.get("id"),
                        "filename": f.get("filename", ""),
                        "name": _split(f.get("filename", ""))[1],
                        "size": f.get("size", 0) or 0,
                        "state": state,
                        "kind": file_kind(state),
                        "failure": failure_label(state),
                        "percent": round(f.get("percentComplete", 0) or 0, 1),
                        "speed": f.get("averageSpeed", 0) or 0,
                        "place": f.get("placeInQueue"),
                    }
                )
                row["size"] += f.get("size", 0) or 0
                row["transferred"] += f.get("bytesTransferred", 0) or 0
    out = []
    for row in rows.values():
        files = row["files"]
        files.sort(key=lambda f: f["name"].lower())
        count = {
            k: sum(1 for f in files if f["kind"] == k)
            for k in ("done", "failed", "active", "queued")
        }
        row["count"] = count
        row["total"] = len(files)
        failed = [f for f in files if f["kind"] == "failed"]
        row["failed_files"] = failed
        if count["active"]:
            row["state"], row["label"] = "downloading", "Downloading"
        elif count["queued"]:
            row["state"], row["label"] = "queued", "Queued"
        elif failed and all(f["failure"] == "Cancelled" for f in failed):
            row["state"], row["label"] = "cancelled", "Cancelled"
        elif failed:
            row["state"] = "failed"
            row["label"] = (
                "Failed" if len(failed) == len(files) else f"{len(failed)} of {len(files)} failed"
            )
        else:
            row["state"], row["label"] = "done", "Done"
        row["speed"] = sum(f["speed"] for f in files if f["kind"] == "active")
        places = [f["place"] for f in files if f["kind"] == "queued" and f["place"]]
        row["place"] = min(places) if places else None
        row["percent"] = round(100 * row["transferred"] / row["size"], 1) if row["size"] else 0
        out.append(row)
    order = {"downloading": 0, "queued": 1, "failed": 2, "cancelled": 3, "done": 4}
    out.sort(key=lambda r: (order[r["state"]], r["folder_name"].lower()))
    return out

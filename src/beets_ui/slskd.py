"""Minimal slskd REST client (search, enqueue downloads, transfer status)."""

from __future__ import annotations

import posixpath
import uuid
from collections import defaultdict
from typing import Any

import httpx

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


class SlskdError(Exception):
    pass


def _split(filename: str) -> tuple[str, str]:
    """Soulseek paths use backslashes; return (directory, basename)."""
    norm = filename.replace("\\", "/")
    return posixpath.dirname(norm), posixpath.basename(norm)


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
            raise SlskdError(f"cannot reach slskd: {e}") from e
        if r.status_code >= 400:
            raise SlskdError(f"slskd {method} {path}: HTTP {r.status_code} {r.text[:200]}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    # ---------------------------------------------------------------- search

    def search(self, text: str) -> str:
        search_id = str(uuid.uuid4())
        self._req("POST", "/searches", json={"id": search_id, "searchText": text})
        return search_id

    def search_state(self, search_id: str) -> dict[str, Any]:
        return self._req("GET", f"/searches/{search_id}")

    def search_responses(self, search_id: str) -> list[dict[str, Any]]:
        return self._req("GET", f"/searches/{search_id}/responses") or []

    def searches(self) -> list[dict[str, Any]]:
        return self._req("GET", "/searches") or []

    def delete_search(self, search_id: str) -> None:
        self._req("DELETE", f"/searches/{search_id}")

    # ------------------------------------------------------------- transfers

    def enqueue(self, username: str, files: list[dict[str, Any]]) -> None:
        body = [{"filename": f["filename"], "size": f["size"]} for f in files]
        self._req("POST", f"/transfers/downloads/{username}", json=body)

    def downloads(self) -> list[dict[str, Any]]:
        return self._req("GET", "/transfers/downloads") or []

    def cancel(self, username: str, transfer_id: str, remove: bool = False) -> None:
        self._req(
            "DELETE",
            f"/transfers/downloads/{username}/{transfer_id}",
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
            folder, name = _split(f.get("filename", ""))
            ext = (f.get("extension") or posixpath.splitext(name)[1].lstrip(".")).lower()
            if audio_only and ext not in AUDIO_EXT:
                continue
            g = groups.setdefault(
                (user, folder),
                {
                    "username": user,
                    "folder": folder,
                    "folder_name": posixpath.basename(folder) or folder,
                    "files": [],
                    "size": 0,
                    "exts": defaultdict(int),
                    "bitrates": [],
                    "free_slot": bool(resp.get("hasFreeUploadSlot")),
                    "queue": resp.get("queueLength", 0),
                    "speed": resp.get("uploadSpeed", 0),
                    "locked": False,
                },
            )
            g["files"].append(
                {
                    "filename": f["filename"],
                    "name": name,
                    "size": f.get("size", 0),
                    "bitrate": f.get("bitRate"),
                    "length": f.get("length"),
                    "ext": ext,
                }
            )
            g["size"] += f.get("size", 0)
            g["exts"][ext] += 1
            if f.get("bitRate"):
                g["bitrates"].append(f["bitRate"])
        for f in resp.get("lockedFiles", []) or []:
            folder, _ = _split(f.get("filename", ""))
            if (user, folder) in groups:
                groups[(user, folder)]["locked"] = True
    out = []
    for g in groups.values():
        g["exts"] = dict(g["exts"])
        g["format"] = "/".join(sorted(g["exts"], key=lambda e: -g["exts"][e])).upper()
        g["bitrate"] = round(sum(g["bitrates"]) / len(g["bitrates"])) if g["bitrates"] else None
        g["lossless"] = any(
            e in {"flac", "wav", "alac", "ape", "wv", "aiff", "dsf"} for e in g["exts"]
        )
        g["files"].sort(key=lambda f: f["name"].lower())
        del g["bitrates"]
        out.append(g)
    out.sort(key=lambda g: (not g["free_slot"], not g["lossless"], g["queue"], -g["speed"]))
    return out


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
                    "folder": folder,
                    "folder_name": posixpath.basename(folder.replace("\\", "/")),
                    "files": [],
                    "size": 0,
                    "transferred": 0,
                },
            )
            for f in d.get("files", []) or []:
                row["files"].append(
                    {
                        "id": f.get("id"),
                        "name": _split(f.get("filename", ""))[1],
                        "state": f.get("state", ""),
                        "percent": round(f.get("percentComplete", 0) or 0, 1),
                    }
                )
                row["size"] += f.get("size", 0) or 0
                row["transferred"] += f.get("bytesTransferred", 0) or 0
    out = []
    for row in rows.values():
        states = [f["state"] for f in row["files"]]
        if all("Succeeded" in s for s in states):
            row["state"] = "done"
        elif any(
            ("Errored" in s or "Rejected" in s or "Cancelled" in s) for s in states
        ) and not any(("InProgress" in s or "Queued" in s or "Initializing" in s) for s in states):
            row["state"] = "failed"
        elif any("InProgress" in s for s in states):
            row["state"] = "downloading"
        else:
            row["state"] = "queued"
        row["percent"] = round(100 * row["transferred"] / row["size"], 1) if row["size"] else 0
        out.append(row)
    order = {"downloading": 0, "queued": 1, "failed": 2, "done": 3}
    out.sort(key=lambda r: (order[r["state"]], r["folder_name"].lower()))
    return out

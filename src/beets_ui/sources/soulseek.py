"""Soulseek, through slskd's API (see ../slskd.py for the client itself)."""

from __future__ import annotations

from typing import Any

from ..slskd import Slskd, flatten_downloads, group_responses
from .base import Source


def search_label(state: str) -> tuple[bool, str]:
    """(still running, label) for a slskd search state."""
    s = str(state or "")
    if "Completed" not in s:
        return True, "Searching"
    if "Cancelled" in s:
        return False, "Stopped"
    return False, "Finished"


class SoulseekSource(Source):
    name = "soulseek"
    label = "Soulseek"

    def __init__(self, client: Slskd):
        self.client = client

    def status(self) -> dict[str, Any]:
        server = self.client.server()
        return {
            "reachable": True,
            "connected": bool(server.get("isLoggedIn", server.get("isConnected"))),
            "error": "",
        }

    def connect(self) -> None:
        self.client.connect()

    def search(self, text: str, wait: int = 15, max_queue: int = 0, min_speed: int = 0) -> str:
        return self.client.search(text, wait=wait, max_queue=max_queue, min_speed=min_speed)

    def _info(self, s: dict[str, Any]) -> dict[str, Any]:
        running, label = search_label(s.get("state", ""))
        return {
            "id": s.get("id", ""),
            "text": s.get("searchText", ""),
            "running": running,
            "label": label,
            "answers": s.get("responseCount") or 0,
            "files": s.get("fileCount") or 0,
            "started": s.get("startedAt", ""),
        }

    def search_info(self, search_id: str) -> dict[str, Any]:
        return self._info(self.client.search_state(search_id) or {"id": search_id})

    def searches(self) -> list[dict[str, Any]]:
        found = [self._info(s) for s in self.client.searches()]
        return sorted(found, key=lambda s: s["started"], reverse=True)

    def stop_search(self, search_id: str) -> None:
        self.client.stop_search(search_id)

    def delete_search(self, search_id: str) -> None:
        self.client.delete_search(search_id)

    def offers(self, search_id: str, audio_only: bool = True) -> list[dict[str, Any]]:
        out = group_responses(self.client.search_responses(search_id), audio_only=audio_only)
        for o in out:
            o["source"] = self.name
        return out

    def listing(self, origin: str, path: str) -> list[dict[str, Any]]:
        return self.client.directory(origin, path)

    def fetch(self, origin: str, files: list[dict[str, Any]]) -> None:
        self.client.enqueue(origin, files)

    def transfers(self) -> list[dict[str, Any]]:
        out = flatten_downloads(self.client.downloads())
        for t in out:
            t["source"] = self.name
        return out

    def cancel(self, origin: str, ids: list[str], remove: bool = False) -> None:
        for tid in ids:
            self.client.cancel(origin, tid, remove=remove)

    def clear_finished(self) -> None:
        self.client.clear_completed()

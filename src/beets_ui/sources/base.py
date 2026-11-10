"""What beets-ui needs from a place it downloads music from.

Today the only source is Soulseek (through slskd). Pages and the Get engine talk
to this interface, so another source (a streaming service ripper, say) can be
added without them changing, and the interface never names the source.

Every method may raise SourceError with a message that can be shown as is.
"""

from __future__ import annotations

from typing import Any


class SourceError(Exception):
    pass


class Source:
    #: Short id, used in URLs and stored with requests ("soulseek").
    name = ""
    #: Shown only in folder details, the home page's status line and settings.
    label = ""

    # ------------------------------------------------------------- connection

    def status(self) -> dict[str, Any]:
        """{"reachable": bool, "connected": bool, "error": str}"""
        raise NotImplementedError

    def connect(self) -> None:
        raise NotImplementedError

    # --------------------------------------------------------------- searches

    def search(self, text: str, wait: int = 15, max_queue: int = 0, min_speed: int = 0) -> str:
        """Start a search; returns its id. Results come in while it runs."""
        raise NotImplementedError

    def search_info(self, search_id: str) -> dict[str, Any]:
        """{"id", "text", "running", "label" (Searching/Finished/Stopped), "answers", "files"}"""
        raise NotImplementedError

    def searches(self) -> list[dict[str, Any]]:
        """Recent searches, newest first, shaped like search_info()."""
        raise NotImplementedError

    def stop_search(self, search_id: str) -> None:
        raise NotImplementedError

    def delete_search(self, search_id: str) -> None:
        raise NotImplementedError

    def offers(self, search_id: str, audio_only: bool = True) -> list[dict[str, Any]]:
        """One offer per folder that can be downloaded, best first.

        Keys: origin (who or where it comes from), folder, path, folder_name, files,
        format, bitrate, lossless, tracks, size, free_slot, queue, speed, locked.
        """
        raise NotImplementedError

    # -------------------------------------------------------------- downloads

    def listing(self, origin: str, path: str) -> list[dict[str, Any]]:
        """Every file in an offer's folder (a search may only return some of them)."""
        raise NotImplementedError

    def fetch(self, origin: str, files: list[dict[str, Any]]) -> None:
        """Download files ({"filename", "size"}) into the inbox."""
        raise NotImplementedError

    def transfers(self) -> list[dict[str, Any]]:
        """Downloads, one row per folder: origin, folder, folder_name, state
        (downloading/queued/failed/cancelled/done), label, percent, speed, place,
        size, files, failed_files."""
        raise NotImplementedError

    def cancel(self, origin: str, ids: list[str], remove: bool = False) -> None:
        raise NotImplementedError

    def clear_finished(self) -> None:
        raise NotImplementedError

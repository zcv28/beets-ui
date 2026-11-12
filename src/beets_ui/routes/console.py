from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request

from .. import beets_ctx
from ..console import ConsoleError
from ..web import flash, redirect, user

router = APIRouter(prefix="/console")

SUGGESTIONS = [
    ("update", "Re-read tags from files that changed on disk"),
    ("move", "Move files to match the path format"),
    ("ls -a", "List albums"),
    ("stats", "Library statistics"),
    ("fields", "List available fields"),
    ("config", "Show the effective configuration"),
    ("version", "Show beets version and loaded plugins"),
    ("fetchart", "Fetch missing album art (fetchart plugin)"),
    ("embedart -y", "Embed album art into files (embedart plugin)"),
    ("lyrics", "Fetch lyrics (lyrics plugin)"),
    ("replaygain -a", "Compute ReplayGain for albums (replaygain plugin)"),
    ("convert -y -a", "Transcode albums (convert plugin)"),
    ("mbsync", "Refresh metadata from MusicBrainz (mbsync plugin)"),
    ("duplicates -a", "Find duplicate albums (duplicates plugin)"),
    ("missing -a", "Find albums with missing tracks (missing plugin)"),
    ("import -q -L <query>", "Re-tag library albums, applying only strong matches"),
]


def context(request: Request, cmd: str = "", open_: bool = False) -> dict:
    """What the console drop-down above the jobs list needs."""
    return {
        "cmd": cmd,
        "console_open": open_ or bool(cmd),
        "suggestions": SUGGESTIONS,
        "plugins": beets_ctx.plugin_names(),
        "console_busy": request.app.state.console.running,
    }


@router.get("")
def console_page(cmd: str = ""):
    """The console is a drop-down on the jobs page now."""
    query = {"console": 1, **({"cmd": cmd} if cmd else {})}
    return redirect("/jobs?" + urlencode(query))


@router.post("")
def console_run(request: Request, cmd: str = Form("")):
    try:
        job = request.app.state.console.submit(cmd, user(request))
    except ConsoleError as e:
        flash(request, str(e), "warn")
        return redirect("/jobs?" + urlencode({"console": 1, "cmd": cmd}))
    return redirect(f"/jobs/{job}")


@router.post("/kill")
def console_kill(request: Request):
    if request.app.state.console.kill():
        flash(request, "Sent interrupt to the running command.")
    else:
        flash(request, "No console command is running.", "wip")
    return redirect("/jobs?console=1")

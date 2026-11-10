from __future__ import annotations

from fastapi import APIRouter, Form, Request

from .. import beets_ctx
from ..console import ConsoleError
from ..web import flash, redirect, render, user

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


@router.get("")
def console_page(request: Request, cmd: str = ""):
    return render(
        request,
        "console.html",
        nav="console",
        cmd=cmd,
        suggestions=SUGGESTIONS,
        recent=request.app.state.state.jobs(limit=20, kind="console"),
        plugins=beets_ctx.plugin_names(),
        console_busy=request.app.state.console.running,
    )


@router.post("")
def console_run(request: Request, cmd: str = Form("")):
    try:
        job = request.app.state.console.submit(cmd, user(request))
    except ConsoleError as e:
        flash(request, str(e), "warn")
        return redirect("/console")
    return redirect(f"/jobs/{job}")


@router.post("/kill")
def console_kill(request: Request):
    if request.app.state.console.kill():
        flash(request, "Sent interrupt to the running command.")
    else:
        flash(request, "No console command is running.", "wip")
    return redirect("/console")

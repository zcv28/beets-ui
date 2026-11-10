from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request

from ..history import UNDOABLE
from ..web import flash, partial, redirect, render, user

router = APIRouter(prefix="/jobs")

MAX_LOG = 200_000  # characters shown in the browser


def _job_or_404(request: Request, job_id: int) -> dict:
    job = request.app.state.state.job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


def _trim(log: str) -> tuple[str, bool]:
    if len(log) <= MAX_LOG:
        return log, False
    return log[-MAX_LOG:], True


@router.get("")
def jobs_page(request: Request):
    return render(request, "jobs.html", nav="jobs", jobs=request.app.state.state.jobs(limit=100))


@router.get("/table")
def jobs_table(request: Request):
    return partial(
        request, "partials/jobs_table.html", jobs=request.app.state.state.jobs(limit=100)
    )


# Finished jobs worth a pop-up: lookups run all the time, so only failed ones.
QUIET = {"preview", "jellyfin-sync"}


@router.get("/pill")
def jobs_pill(request: Request, since: float = 0):
    """Nav badge plus pop-ups for jobs that finished since the last poll."""
    jobs = request.app.state.jobs
    since = since or time.time()
    done = request.app.state.state.jobs_finished_since(since)
    if done:
        since = max(j["finished_at"] for j in done)
    toasts = [j for j in done if j["kind"] not in QUIET or j["status"] == "failed"]
    # Downloads that finished or failed, as seen by the source watcher.
    watch = getattr(request.app.state, "source_watch", None)
    slsk = watch.events_since(since) if watch else []
    if slsk:
        since = max([since, *(e["at"] for e in slsk)])
    # Requests (Get) that finished, failed or need you.
    asked = [
        w
        for w in request.app.state.state.wanted_changed_since(since)
        if w["status"] in ("done", "failed", "choosing", "review")
    ]
    if asked:
        since = max([since, *(w["updated_at"] for w in asked)])
    return partial(
        request,
        "partials/jobs_pill.html",
        running=len(jobs.running()),
        pending=jobs.pending(),
        since=since,
        toasts=toasts,
        slsk_toasts=slsk,
        wanted_toasts=asked,
        oob=True,
    )


@router.get("/{job_id}")
def job_page(request: Request, job_id: int):
    job = _job_or_404(request, job_id)
    log, trimmed = _trim(job["log"])
    snap = request.app.state.state.snapshot_for_job(job_id) if job["kind"] in UNDOABLE else None
    undone = request.app.state.state.job(snap["undone_by"]) if snap and snap["undone_by"] else None
    return render(
        request,
        "job.html",
        nav="jobs",
        job=job,
        log=log,
        trimmed=trimmed,
        snap=snap,
        undone=undone,
    )


@router.post("/{job_id}/undo")
def job_undo(request: Request, job_id: int):
    job = _job_or_404(request, job_id)
    snap = request.app.state.state.snapshot_for_job(job_id)
    if snap is None or job["status"] not in ("done", "failed"):
        flash(request, "This job has nothing to undo.", "wip")
        return redirect(f"/jobs/{job_id}")
    if snap["undone_by"]:
        flash(request, f"Already undone by job #{snap['undone_by']}.", "wip")
        return redirect(f"/jobs/{job_id}")
    undo = request.app.state.jobs.submit(
        "undo", f"Undo #{job_id}: {job['title']}", {"snapshot_id": snap["id"]}, user(request)
    )
    return redirect(f"/jobs/{undo}")


@router.get("/{job_id}/log")
def job_log(request: Request, job_id: int):
    job = _job_or_404(request, job_id)
    log, trimmed = _trim(job["log"])
    resp = partial(request, "partials/job_log.html", job=job, log=log, trimmed=trimmed)
    if job["status"] not in ("queued", "running"):
        resp.headers["HX-Refresh"] = "true"
    return resp


@router.post("/{job_id}/cancel")
def job_cancel(request: Request, job_id: int):
    job = _job_or_404(request, job_id)
    if request.app.state.jobs.cancel(job_id):
        flash(request, f"Job #{job_id} cancelled.")
    elif job["status"] == "running" and job["kind"] == "console":
        request.app.state.console.kill()
        flash(request, f"Sent interrupt to job #{job_id}.")
    else:
        flash(request, "Only queued jobs (or running console commands) can be cancelled.", "wip")
    return redirect(f"/jobs/{job_id}")

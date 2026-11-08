from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..web import flash, partial, redirect, render

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


@router.get("/{job_id}")
def job_page(request: Request, job_id: int):
    job = _job_or_404(request, job_id)
    log, trimmed = _trim(job["log"])
    return render(request, "job.html", nav="jobs", job=job, log=log, trimmed=trimmed)


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

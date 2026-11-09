"""Run arbitrary non-interactive `beet` commands as jobs."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading

from .jobs import JobContext, JobQueue

INTERACTIVE = {("config", "-e"), ("config", "--edit"), ("edit",)}


class ConsoleError(ValueError):
    pass


def parse(command: str) -> list[str]:
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise ConsoleError(f"cannot parse command: {e}") from e
    if argv and argv[0] in ("beet", "beets"):
        argv = argv[1:]
    if not argv:
        raise ConsoleError("empty command")
    for combo in INTERACTIVE:
        if argv[0] == combo[0] and all(c in argv for c in combo[1:]):
            raise ConsoleError(f"'beet {' '.join(combo)}' is interactive and cannot run here")
    return argv


def beet_executable() -> list[str]:
    exe = shutil.which("beet")
    if exe:
        return [exe]
    return [sys.executable, "-m", "beets"]


class Console:
    def __init__(self, jobs: JobQueue, beetsdir: str):
        self.jobs = jobs
        self.beetsdir = beetsdir
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        jobs.register("console")(self._job)

    def submit(self, command: str, user: str | None = None) -> int:
        return self.submit_argv(parse(command), user)

    def submit_argv(self, argv: list[str], user: str | None = None) -> int:
        return self.jobs.submit("console", "beet " + shlex.join(argv), {"argv": argv}, user)

    def kill(self) -> bool:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.send_signal(signal.SIGINT)
                return True
        return False

    def _job(self, ctx: JobContext) -> None:
        argv = ctx.params["argv"]
        env = {**os.environ, "BEETSDIR": self.beetsdir, "PYTHONUNBUFFERED": "1", "TERM": "dumb"}
        ctx.log("$ beet " + shlex.join(argv))
        proc = subprocess.Popen(
            [*beet_executable(), *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            errors="replace",
            bufsize=1,
        )
        with self._lock:
            self._proc = proc
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                ctx.log(line)
            code = proc.wait()
        finally:
            with self._lock:
                self._proc = None
        ctx.log(f"[exit code {code}]")
        if code != 0:
            raise RuntimeError(f"beet exited with code {code}")

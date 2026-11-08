"""Loads beets (config, plugins, library) once for the whole process."""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from beets import config, plugins
from beets import context as beets_context
from beets import library as beets_library

log = logging.getLogger("beets_ui")

_lib: beets_library.Library | None = None
_init_lock = threading.Lock()
_override_lock = threading.RLock()


def init(beetsdir: Path | str | None = None, load_plugins: bool = True) -> beets_library.Library:
    """Read the beets config from BEETSDIR, load plugins, open the library."""
    global _lib
    with _init_lock:
        if _lib is not None:
            return _lib
        if beetsdir:
            os.environ["BEETSDIR"] = str(beetsdir)
            Path(beetsdir).mkdir(parents=True, exist_ok=True)
        config.read()
        # Beets logs through its own logger; make sure INFO records reach
        # our per-job handler without printing everything to stdout.
        logging.getLogger("beets").setLevel(logging.INFO)
        if load_plugins:
            plugins.load_plugins()
        dbpath = config["library"].as_path()
        Path(dbpath).parent.mkdir(parents=True, exist_ok=True)
        directory = config["directory"].as_filename()
        _lib = beets_library.Library(dbpath, directory)
        _lib.get_item(0)  # test connection
        plugins.send("library_opened", lib=_lib)
        log.info("beets library %s (music dir %s)", dbpath, directory)
        return _lib


def lib() -> beets_library.Library:
    """Return the library, making sure this thread can resolve its paths.

    beets >= 2.13 stores paths relative to the music directory and expands
    them through a ContextVar that ``Library()`` only sets on the thread that
    opened it. Request threads and the job thread start with an empty
    context, so without this every ``item.path``/``album.artpath`` read there
    would come back relative (and every file operation would miss).
    """
    if _lib is None:
        raise RuntimeError("beets has not been initialised")
    if beets_context.get_music_dir() != _lib.directory:
        beets_context.set_music_dir(_lib.directory)
    return _lib


def reset() -> None:
    """Forget the open library (tests)."""
    global _lib
    if _lib is not None:
        with contextlib.suppress(Exception):
            _lib._close()
    _lib = None


def library_dir() -> Path:
    return Path(os.fsdecode(lib().directory))


@contextlib.contextmanager
def config_overrides(values: dict[str, Any]) -> Iterator[None]:
    """Temporarily overlay config values; removes every source added meanwhile.

    beets' importer mutates the global config while it runs (``set_config``),
    so we also discard those layers afterwards to keep runs independent.
    """
    with _override_lock:
        before = len(config.sources)
        if values:
            config.set(values)
        try:
            yield
        finally:
            added = len(config.sources) - before
            if added > 0:
                del config.sources[:added]


def art_filename() -> str:
    return config["art_filename"].as_str()


def plugin_names() -> list[str]:
    return [p.name for p in plugins.find_plugins()]

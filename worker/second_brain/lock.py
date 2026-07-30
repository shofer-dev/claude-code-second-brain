"""File locking, with the two postures this plugin needs.

`flock` is the only coordination primitive here — there is no daemon and nothing
listening, so exactly-once delivery and single-worker-per-session are both file
operations rather than promises (DESIGN.md §Exactly-once is a file operation).

Two postures, deliberately different:

- **`held()`** — the worker's session lock. Acquired once, non-blocking, and kept
  for the process's lifetime; whoever holds it *is* the session's worker, so the
  two incarnations of the monitor's worker cannot both run.
- **`claim()`** — a short exclusive section around a mailbox rewrite. Never
  blocks: on contention the caller does nothing, because the other channel is by
  definition mid-delivery.
"""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

from . import paths


def held(path: Path) -> IO[str] | None:
    """Take an exclusive lock and keep it. Returns None if someone else holds it.

    The returned handle must stay referenced for as long as the lock is wanted —
    closing it, or exiting the process, releases it.
    """
    paths.ensure_dir(path.parent)
    fh = open(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600), "w", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


@contextmanager
def claim(path: Path) -> Iterator[bool]:
    """Best-effort exclusive section. Yields False on contention; never blocks."""
    paths.ensure_dir(path.parent)
    fh = None
    try:
        fh = open(os.open(str(path) + ".lock", os.O_WRONLY | os.O_CREAT, 0o600), "w", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    except OSError:
        yield False
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()

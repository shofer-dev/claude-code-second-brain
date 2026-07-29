"""The spool: the one-way pipe from the hooks to the worker.

Hooks are stateless and must finish in milliseconds, so they do exactly one thing
with what they project — append it here — and exit 0. The worker tails the file.
Nothing acknowledges anything: if no worker is running the spool simply grows and
the session is untouched (DESIGN.md §Failure modes, "fail open, always").

One `O_APPEND` write per observation is atomic between processes, so several hooks
firing concurrently need no lock. The reader keeps its own byte offset in memory —
losing it re-reads the spool from the start, which is why the worker seeks to the
end when it attaches to a session already in progress.
"""
from __future__ import annotations

from pathlib import Path

from . import paths
from .projection import Observation


def append(session_id: str, observations: list[Observation]) -> int:
    """Append observations; returns how many characters of body were written."""
    if not observations:
        return 0
    path = paths.spool_path(session_id)
    written = 0
    for obs in observations:
        paths.append_private(path, obs.to_json())
        written += obs.kept_chars
    return written


class SpoolReader:
    """Tails one session's spool, yielding whole observations only."""

    def __init__(self, session_id: str, start_at_end: bool = False) -> None:
        self.path: Path = paths.spool_path(session_id)
        self.offset = 0
        if start_at_end:
            try:
                self.offset = self.path.stat().st_size
            except OSError:
                self.offset = 0

    def read(self) -> list[Observation]:
        """Return observations appended since the last read."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:      # spool was rotated or cleared
            self.offset = 0
        if size == self.offset:
            return []
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read(size - self.offset)
        except OSError:
            return []

        consumed = chunk.rfind(b"\n") + 1
        if consumed <= 0:
            return []
        self.offset += consumed

        out: list[Observation] = []
        for line in chunk[:consumed].splitlines():
            if not line.strip():
                continue
            obs = Observation.from_json(line.decode("utf-8", "replace"))
            if obs is not None:
                out.append(obs)
        return out


def clear(session_id: str) -> None:
    """Drop a finished session's spool — it is a pipe, never a record."""
    paths.spool_path(session_id).unlink(missing_ok=True)

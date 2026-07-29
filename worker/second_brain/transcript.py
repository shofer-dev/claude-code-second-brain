"""Reading the primary's transcript forward from a durable offset.

The hook payload is the *clock*; the transcript JSONL is the *content*, because
the assistant's narration between tool calls appears nowhere else and it is the
highest-value 13 % of the stream (DESIGN.md §Why the transcript rather than the
hook payload). Reading it is a local file read, so it is free — only what the
projection forwards ever costs anything.

The offset is durable and per session. Two hazards it must survive, both handled
by re-seeking rather than by re-reading: a transcript that shrank (a new file at
the same path) and a partial last line (the harness is mid-write). Losing an
offset costs a gap in observation, never a duplicate and never a crash.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths


@dataclass
class Cursor:
    offset: int = 0
    size: int = 0

    @classmethod
    def load(cls, session_id: str) -> Cursor:
        try:
            data = json.loads(paths.offset_path(session_id).read_text(encoding="utf-8"))
            return cls(offset=int(data.get("offset", 0)), size=int(data.get("size", 0)))
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, session_id: str) -> None:
        paths.write_private(paths.offset_path(session_id),
                            json.dumps({"offset": self.offset, "size": self.size}))


def read_new_records(transcript_path: str | Path, session_id: str) -> list[dict[str, Any]]:
    """Return transcript records appended since the last call, advancing the cursor.

    A trailing partial line is left unconsumed: the offset stops at the last
    newline, so the next call picks the record up whole.
    """
    path = Path(transcript_path)
    try:
        size = path.stat().st_size
    except OSError:
        return []

    cursor = Cursor.load(session_id)
    if size < cursor.offset:
        # The file shrank: a different transcript now lives at this path. Start at
        # its end rather than re-projecting a session we may already have seen.
        cursor.offset = size
        cursor.save(session_id)
        return []
    if size == cursor.offset:
        return []

    try:
        with path.open("rb") as fh:
            fh.seek(cursor.offset)
            chunk = fh.read(size - cursor.offset)
    except OSError:
        return []

    consumed = chunk.rfind(b"\n") + 1
    if consumed <= 0:
        return []

    records: list[dict[str, Any]] = []
    for line in chunk[:consumed].splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)

    cursor.offset += consumed
    cursor.size = size
    cursor.save(session_id)
    return records


def seek_to_end(transcript_path: str | Path, session_id: str) -> None:
    """Point the cursor at the current end — used when a session is first enrolled."""
    try:
        size = Path(transcript_path).stat().st_size
    except OSError:
        size = 0
    Cursor(offset=size, size=size).save(session_id)

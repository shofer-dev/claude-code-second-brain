"""The status file: how the human surfaces work with no service to query.

There is no endpoint, so `/second-brain-stats`, `/second-brain-why` and the
statusline are file reads. The worker writes this through on every pass, which
means a stats read is **stale-but-readable when no worker is running** — and it
says so with its timestamp rather than pretending to be live.

The one thing this loses against a queryable service is liveness: a read cannot
force a worker to answer, so what you see is as fresh as its last pass. Given the
pass cadence is minutes by design, that is the natural resolution anyway.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import paths


@dataclass
class Status:
    """One session worker's live numbers."""

    session_id: str = ""
    task_id: str = ""
    workspace: str = ""
    cwd: str = ""
    state: str = "starting"           # watching | thinking | muted | silent (budget) | stopped
    pid: int = 0
    hosted_by: str = ""               # monitor | hook
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    passes: int = 0
    last_pass_at: float = 0.0
    last_pass_s: float = 0.0
    pending_chars: int = 0
    window_chars: int = 0
    window_fill: float = 0.0
    compactions: int = 0

    observed_chars: int = 0
    observed_raw_chars: int = 0
    by_tool: dict[str, list[int]] = field(default_factory=dict)   # tool → [raw, kept]

    tokens: dict[str, int] = field(default_factory=lambda: {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
    primary_tokens: int = 0
    budget_task_used: int = 0
    budget_hour_used: int = 0

    advisories_generated: int = 0
    advisories_delivered: int = 0
    advisories_dropped: int = 0
    detectors: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_feedback: list[str] = field(default_factory=list)
    mcp: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    note: str = ""

    def observe(self, tool: str, raw: int, kept: int) -> None:
        bucket = self.by_tool.setdefault(tool or "text", [0, 0])
        bucket[0] += raw
        bucket[1] += kept
        self.observed_raw_chars += raw
        self.observed_chars += kept

    def detector(self, name: str) -> dict[str, Any]:
        return self.detectors.setdefault(name, {
            "runs": 0, "advised": 0, "timeouts": 0, "errors": 0, "delivered": 0,
            "adopted": 0, "state": "active",
        })

    def save(self) -> None:
        self.updated_at = time.time()
        try:
            paths.write_private(paths.status_path(self.session_id),
                                json.dumps(asdict(self), ensure_ascii=False, indent=1))
        except OSError:
            pass


def read(session_id: str) -> dict[str, Any] | None:
    try:
        data = json.loads(paths.status_path(session_id).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def read_all(max_age_s: float = 86400) -> list[dict[str, Any]]:
    """Every session's status, newest first — what `/second-brain-stats` opens with."""
    out: list[dict[str, Any]] = []
    now = time.time()
    for path in paths.status_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and now - float(data.get("updated_at", 0)) <= max_age_s:
            out.append(data)
    return sorted(out, key=lambda d: float(d.get("updated_at", 0)), reverse=True)

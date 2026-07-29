"""The task ledger: what survives compaction, and dies with the task.

Scope is the **task**, not the workspace, and that is a correctness decision rather
than a filing one (DESIGN.md §The ledger is per task). A workspace-scoped ledger
would accumulate confident, undetectably stale claims, because the repo drifts
under it — other sessions, other people, a pull, a branch switch — and this plugin
watches emissions, not the filesystem. Holding durable codebase facts safely needs
invalidation machinery this plugin deliberately does not have.

What a task ledger holds is *judgment about work in progress*: the goal, what was
decided and why, what has already been advised (so it is not advised again), what
was tried and abandoned. All of it is worthless the moment the task is over, so it
expires — on dormancy TTL, on per-workspace count, and on total bytes, whichever
binds first. Deleting one is always safe: it is derived state that a running task
rebuilds from its next observations.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import paths


@dataclass
class Ledger:
    """One task's distilled judgment."""

    task_id: str
    workspace: str = ""
    goal: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    entries: list[dict[str, Any]] = field(default_factory=list)
    advised: list[dict[str, Any]] = field(default_factory=list)
    suppressed: dict[str, str] = field(default_factory=dict)
    calibration: dict[str, dict[str, int]] = field(default_factory=dict)

    # ── persistence ─────────────────────────────────────────────────────────
    @classmethod
    def load(cls, task_id: str, workspace: str = "") -> Ledger:
        try:
            data = json.loads(paths.ledger_path(task_id).read_text(encoding="utf-8"))
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            ledger = cls(**known)
            ledger.task_id = task_id
            if workspace:
                ledger.workspace = workspace
            return ledger
        except (OSError, ValueError, TypeError):
            return cls(task_id=task_id, workspace=workspace)

    def save(self, max_entries: int = 60) -> None:
        self.updated_at = time.time()
        if len(self.entries) > max_entries:
            # Oldest first: what leaves has already been summarised into what stays.
            self.entries = self.entries[-max_entries:]
        self.advised = self.advised[-200:]
        paths.write_private(paths.ledger_path(self.task_id),
                            json.dumps(asdict(self), ensure_ascii=False, indent=1))

    def fork_to(self, new_task_id: str) -> Ledger:
        """Copy for a `SessionStart source: fork` — both sides then diverge."""
        clone = Ledger(**{**asdict(self), "task_id": new_task_id})
        clone.created_at = time.time()
        clone.save()
        return clone

    # ── content ─────────────────────────────────────────────────────────────
    def add_entry(self, text: str, kind: str = "note") -> None:
        text = text.strip()
        if text:
            self.entries.append({"at": time.time(), "kind": kind, "text": text})

    def record_advice(self, advice_id: str, detector: str, headline: str, dedup_key: str) -> None:
        self.advised.append({"id": advice_id, "detector": detector, "headline": headline,
                             "dedup_key": dedup_key, "at": time.time(), "verdict": ""})

    def close_advice(self, advice_id: str, verdict: str, evidence: list[str]) -> None:
        for record in reversed(self.advised):
            if record["id"] == advice_id:
                record["verdict"] = verdict
                record["verdict_evidence"] = evidence[:4]
                record["closed_at"] = time.time()
                # Explicit rejection is the single most effective noise control
                # there is: re-advising something the primary declined is the
                # fastest way to teach it to skip the channel.
                if verdict in {"rejected", "contradicted"} and record.get("dedup_key"):
                    self.suppressed[record["dedup_key"]] = verdict
                self._calibrate(record.get("detector", ""), verdict)
                return

    def _calibrate(self, detector: str, verdict: str) -> None:
        if not detector:
            return
        stats = self.calibration.setdefault(detector, {"delivered": 0, "adopted": 0, "rejected": 0})
        stats["delivered"] += 1
        if verdict in {"adopted", "partially_adopted"}:
            stats["adopted"] += 1
        elif verdict in {"rejected", "contradicted"}:
            stats["rejected"] += 1

    def uptake(self, detector: str) -> float | None:
        """Adopted ÷ delivered for one detector, or None before there is a signal."""
        stats = self.calibration.get(detector)
        if not stats or stats.get("delivered", 0) < 3:
            return None
        return stats.get("adopted", 0) / max(1, stats["delivered"])

    def digest(self, limit: int = 6000) -> str:
        """The ledger as the window's first block. Changes only at a compaction."""
        lines: list[str] = []
        if self.goal:
            lines.append(f"Task goal: {self.goal}")
        if self.entries:
            lines.append("\nWhat has been established so far:")
            lines += [f"- {e['text']}" for e in self.entries]
        open_advice = [a for a in self.advised if not a.get("verdict")]
        if open_advice:
            lines.append("\nAdvice already sent and not yet adjudicated:")
            lines += [f"- [{a['id']}] ({a['detector']}) {a['headline']}" for a in open_advice[-10:]]
        closed = [a for a in self.advised if a.get("verdict")]
        if closed:
            lines.append("\nAdvice already adjudicated (do not repeat these):")
            lines += [f"- ({a['detector']}) {a['headline']} → {a['verdict']}" for a in closed[-10:]]
        if self.suppressed:
            lines.append("\nSuppressed findings (the agent declined these — never raise them again):")
            lines += [f"- {key} ({why})" for key, why in list(self.suppressed.items())[-10:]]
        text = "\n".join(lines).strip()
        return (text[:limit] + "…") if len(text) > limit else text


# ── garbage collection ──────────────────────────────────────────────────────
def sweep(*, ttl_days: float, max_per_workspace: int, max_bytes: int,
          active: set[str] | None = None) -> list[str]:
    """Delete expired and over-cap ledgers. Returns the task ids removed.

    Explicit rather than implied: a ledger nobody resumed within the TTL is an
    abandoned task, and the caps are the backstop for the pathological case a TTL
    alone does not bound (a script starting hundreds of sessions). An active task
    is never swept, and every deletion is logged — silent data disappearance is
    indistinguishable from a bug.
    """
    active = active or set()
    directory = paths.ledger_dir()
    now = time.time()
    ttl_s = ttl_days * 86400
    removed: list[str] = []

    records: list[tuple[float, int, str, Any]] = []
    for path in directory.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            size = path.stat().st_size
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        task_id = str(data.get("task_id") or path.stem)
        updated = float(data.get("updated_at", 0.0))
        if task_id in active:
            continue
        if now - updated > ttl_s:
            path.unlink(missing_ok=True)
            removed.append(task_id)
            continue
        records.append((updated, size, task_id, (path, str(data.get("workspace") or ""))))

    by_workspace: dict[str, list[tuple[float, int, str, Any]]] = {}
    for record in records:
        by_workspace.setdefault(record[3][1], []).append(record)
    for group in by_workspace.values():
        group.sort(key=lambda r: r[0], reverse=True)          # newest first
        for updated, _size, task_id, (path, _ws) in group[max_per_workspace:]:
            path.unlink(missing_ok=True)
            removed.append(task_id)

    survivors = [r for r in records if r[2] not in removed]
    total = sum(r[1] for r in survivors)
    survivors.sort(key=lambda r: r[0])                        # oldest first
    for updated, size, task_id, (path, _ws) in survivors:
        if total <= max_bytes:
            break
        path.unlink(missing_ok=True)
        removed.append(task_id)
        total -= size
    return removed


def forget(scope: str, *, task_id: str = "", workspace: str = "") -> list[str]:
    """`/second-brain-forget`: drop one task's ledger, a workspace's, or all."""
    removed: list[str] = []
    if scope == "task" and task_id:
        path = paths.ledger_path(task_id)
        if path.exists():
            path.unlink()
            removed.append(task_id)
        return removed
    for path in paths.ledger_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if scope == "all" or (scope == "workspace" and str(data.get("workspace") or "") == workspace):
            removed.append(str(data.get("task_id") or path.stem))
            path.unlink(missing_ok=True)
    return removed

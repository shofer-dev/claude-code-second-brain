"""The live cross-task index — the one thing that crosses tasks.

Task isolation is the rule, and there is exactly one justified exception: **another
live task editing the same files right now**. It is plausibly the single most
useful thing this plugin can say, and it is invisible to the primary, which has no
way to know another agent is in its checkout.

It survives the objection that killed the workspace ledger because **it is live,
not remembered**: nothing is retained and later asserted, the question is only what
is happening at this instant, and every entry expires on a TTL. A crashed worker
therefore leaves nothing that outlives it — the index is self-healing by design
rather than by cleanup.

Shared state does not imply a shared process, which is what an earlier draft of the
design got wrong. This is a few hundred bytes of paths and timestamps, append-mostly
and tolerant of staleness: one `O_APPEND` line needs no lock, readers drop expired
entries as they go, and a periodic `flock`ed rewrite is the only locked operation.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from . import paths
from .lock import claim

SAME_CHECKOUT = "same_checkout"
SEPARATE_WORKTREE = "separate_worktree"


@dataclass
class Entry:
    task: str
    cwd: str
    git_dir: str
    goal: str
    paths: list[str]
    at: float

    @classmethod
    def parse(cls, line: str) -> Entry | None:
        try:
            data = json.loads(line)
        except ValueError:
            return None
        if not isinstance(data, dict) or not data.get("task"):
            return None
        return cls(task=str(data["task"]), cwd=str(data.get("cwd", "")),
                   git_dir=str(data.get("git_dir", "")), goal=str(data.get("goal", "")),
                   paths=[str(p) for p in (data.get("paths") or [])], at=float(data.get("at", 0.0)))


def git_common_dir(cwd: str) -> str:
    """The shared git directory: identical for worktrees of one repository."""
    try:
        result = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=cwd or ".",
                                capture_output=True, text=True, timeout=3, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return os.path.realpath(os.path.join(cwd or ".", result.stdout.strip()))
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def publish(workspace: str, *, task: str, cwd: str, git_dir: str, goal: str,
            touched: list[str], max_paths: int = 40) -> None:
    """Append this task's current footprint. One atomic write, no lock."""
    line = json.dumps({
        "task": task, "cwd": cwd, "git_dir": git_dir, "goal": goal[:120],
        "paths": touched[-max_paths:], "at": time.time(),
    }, ensure_ascii=False, separators=(",", ":"))
    try:
        paths.append_private(paths.index_path(workspace), line)
    except OSError:
        pass


def live(workspace: str, *, ttl_s: float, exclude_task: str = "") -> list[Entry]:
    """The newest entry per other live task. Staleness is tolerated by construction."""
    try:
        raw = paths.index_path(workspace).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    now = time.time()
    newest: dict[str, Entry] = {}
    for line in raw:
        entry = Entry.parse(line)
        if entry is None or entry.task == exclude_task or now - entry.at > ttl_s:
            continue
        previous = newest.get(entry.task)
        if previous is None or entry.at > previous.at:
            newest[entry.task] = entry
    return sorted(newest.values(), key=lambda e: e.at, reverse=True)


def collisions(workspace: str, *, task: str, cwd: str, git_dir: str, touched: list[str],
               ttl_s: float) -> list[dict[str, Any]]:
    """Paths another live task is also touching, classified by topology.

    The trigger is **structural, not a judgment**: a path match is computed here
    and the model is only asked to write the advisory, so it cannot hallucinate a
    collision. The two cases differ in urgency, not in whether they are real —
    same checkout means a later writer wins silently; separate worktrees mean two
    branches diverging, which git will report at merge time.
    """
    mine = {p for p in touched if p}
    if not mine:
        return []
    out: list[dict[str, Any]] = []
    for entry in live(workspace, ttl_s=ttl_s, exclude_task=task):
        shared = sorted(mine & set(entry.paths))
        if not shared:
            continue
        case = SAME_CHECKOUT if (entry.cwd and cwd and os.path.realpath(entry.cwd)
                                 == os.path.realpath(cwd)) else SEPARATE_WORKTREE
        if case == SEPARATE_WORKTREE and git_dir and entry.git_dir and git_dir != entry.git_dir:
            continue        # a different repository entirely: not a collision at all
        out.append({"task": entry.task, "goal": entry.goal, "case": case,
                    "paths": shared[:5], "cwd": entry.cwd,
                    "age_s": int(time.time() - entry.at)})
    return out


def compact(workspace: str, *, ttl_s: float) -> int:
    """Rewrite the file without expired entries. The only locked operation here."""
    path = paths.index_path(workspace)
    with claim(path) as locked:
        if not locked:
            return 0        # another worker is compacting; defer, never block
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        now = time.time()
        keep = [line for line in lines
                if (entry := Entry.parse(line)) is not None and now - entry.at <= ttl_s]
        if len(keep) == len(lines):
            return 0
        paths.write_private(path, "\n".join(keep) + ("\n" if keep else ""))
        return len(lines) - len(keep)


def describe(collision: dict[str, Any]) -> str:
    """The structural evidence handed to the detector, ready to be written up."""
    where = ("the same checkout" if collision["case"] == SAME_CHECKOUT
             else "a separate worktree of this repository")
    goal = f" (its goal: {collision['goal']})" if collision.get("goal") else ""
    return (f"Another live task {collision['task']}{goal} is working in {where} and has touched "
            f"{', '.join(collision['paths'])} in the last {collision['age_s']}s.")

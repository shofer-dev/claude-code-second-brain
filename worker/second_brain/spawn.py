"""Lazily starting the session worker where monitors are unavailable.

The worker is normally hosted by the plugin's **monitor** — Claude Code starts it
at session start, it lives exactly as long as the session, and its stdout is the
push channel. On platforms without monitors (Bedrock, Agent Platform, Foundry, or
with telemetry disabled) nothing starts it, so the feed hook does, detached and
guarded by the same per-session lockfile: exactly one worker exists either way.

That path is strictly worse and it is worth naming why rather than pretending it
is equivalent — no push, no waking a stopped loop, and a lifetime bounded by
nothing more graceful than the session's file handles. It is the concrete cost of
running where monitors are absent (DESIGN.md §The monitor channel).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from . import paths
from .lock import held

GRACE_S = 15.0
"""How long hook spawning defers to the monitor at session start.

The monitor is the strictly better host — it owns the push channel — but it
takes a couple of seconds to boot while the first hook event fires immediately.
Spawning inside that window used to win the lock race and cost the session its
push channel (the losing monitor now stands by, but the incumbent hook worker
would hold the lock for the session's life). Where monitors never come (headless
platforms), the only cost is the first worker starting this much later, with the
spool buffering everything in the meantime."""


def worker_running(session_id: str) -> bool:
    """True if some process holds this session's worker lock."""
    handle = held(paths.lock_path(session_id))
    if handle is None:
        return True
    handle.close()          # releasing immediately: we only asked, we do not want it
    return False


def grace_pending(session_id: str) -> bool:
    """True while the monitor's head start is still running for this session.

    The first hook event stamps a first-seen marker and declines to spawn;
    spawning resumes once the marker is older than `GRACE_S`. A worker that
    dies mid-session is respawned immediately — its marker is long past grace.
    """
    grace = float(os.environ.get("SECOND_BRAIN_SPAWN_GRACE_S", GRACE_S))
    if grace <= 0:
        return False
    marker = paths.data_dir() / "state" / f"{paths.safe_name(session_id)}.first-seen"
    try:
        return time.time() - marker.stat().st_mtime < grace
    except OSError:
        try:
            paths.write_private(marker, str(time.time()))
        except OSError:
            pass
        return True


def ensure_worker(session_id: str, cwd: str, transcript_path: str) -> bool:
    """Start a detached worker if none holds the lock. Returns True if one was started.

    Failure to spawn is not an error: the hook has already spooled its
    observations, and a session with no worker is simply a session with no advice.
    """
    if os.environ.get("SECOND_BRAIN_NO_SPAWN"):
        # Hooks-only mode: spool observations, start nothing. Used by the test
        # harness and by anyone who wants the feed without the observer.
        return False
    if worker_running(session_id):
        return False
    if grace_pending(session_id):
        return False
    run_py = Path(__file__).resolve().parent.parent / "run.py"
    if not run_py.exists():
        return False

    env = dict(os.environ)
    env["SECOND_BRAIN_SESSION_ID"] = session_id
    env["SECOND_BRAIN_CWD"] = cwd or ""
    env["SECOND_BRAIN_TRANSCRIPT"] = transcript_path or ""
    env["SECOND_BRAIN_HOSTED_BY"] = "hook"

    log = paths.log_path()
    try:
        paths.ensure_dir(log.parent)
        with open(log, "a", encoding="utf-8") as sink:
            subprocess.Popen(                                    # noqa: S603
                [sys.executable or "python3", str(run_py)],
                stdin=subprocess.DEVNULL, stdout=sink, stderr=sink,
                start_new_session=True, env=env, cwd=cwd or None,
            )
        return True
    except (OSError, ValueError):
        return False

"""The session worker: what the monitor hosts, and what a hook spawns if it cannot.

There is no daemon. This process lives exactly as long as the session it watches,
holds that session's lock so only one of it exists, runs the observer loop, and —
when it is the monitor — pushes advisories by writing one line to stdout.

The lock is the whole coordination story. Whoever holds it *is* the worker, so the
monitor-hosted process and the hook-spawned fallback cannot both run, and a crashed
worker simply releases it for the next hook to notice (DESIGN.md §Lifecycle).

Session identity comes from the environment: Claude Code hands `CLAUDE_CODE_SESSION_ID`
to monitor processes, which is why no process-ancestry join or cwd heuristic is
needed and why two sessions on one repository cannot cross-deliver.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from . import paths
from .config import Config
from .lock import held
from .loop import Observer


def session_id() -> str:
    for key in ("CLAUDE_CODE_SESSION_ID", "SECOND_BRAIN_SESSION_ID", "CLAUDE_SESSION_ID"):
        value = os.environ.get(key)
        if value:
            return value
    return ""


def working_dir() -> str:
    for key in ("SECOND_BRAIN_CWD", "CLAUDE_PROJECT_DIR"):
        value = os.environ.get(key)
        if value and Path(value).is_dir():
            return value
    return os.getcwd()


def setup_logging() -> logging.Logger:
    log = logging.getLogger("second-brain")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    try:
        paths.ensure_dir(paths.log_path().parent)
        handler: logging.Handler = logging.FileHandler(paths.log_path(), encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(process)d] %(message)s", "%H:%M:%S"))
    log.addHandler(handler)
    log.propagate = False
    return log


def make_emitter(hosted_by: str, log: logging.Logger) -> Any:
    """Under a monitor, one stdout line is one notification to the session."""
    def emit(text: str) -> None:
        log.info("push: %s", text[:200])
        if hosted_by == "monitor":
            sys.stdout.write(text.rstrip("\n") + "\n")
            sys.stdout.flush()
    return emit


async def _run(observer: Observer, log: logging.Logger) -> None:
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except (NotImplementedError, ValueError, OSError):
            pass

    task = asyncio.create_task(observer.run())
    stop_task = asyncio.create_task(stopping.wait())
    done, _ = await asyncio.wait({task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    if task not in done:
        log.info("signalled; shutting the observer down")
        observer.finished = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
    stop_task.cancel()


def main() -> int:
    log = setup_logging()
    sid = session_id()
    if not sid:
        log.warning("no session id in the environment; nothing to watch")
        return 0

    lock = held(paths.lock_path(sid))
    if lock is None:
        log.info("another worker already holds session %s; exiting", sid)
        return 0

    cwd = working_dir()
    workspace = paths.workspace_key(cwd)
    cfg = Config.load(workspace)
    if not cfg.observing(workspace):
        # Enrolment is a decision, not a surprise: an unenrolled workspace gets no
        # worker at all, not a silent one.
        log.info("workspace %s is not enrolled; exiting", workspace)
        lock.close()
        return 0

    hosted_by = os.environ.get("SECOND_BRAIN_HOSTED_BY", "monitor")
    observer = Observer(sid, cwd, workspace, emit=make_emitter(hosted_by, log),
                        log=log, hosted_by=hosted_by)
    try:
        asyncio.run(_run(observer, log))
    except KeyboardInterrupt:
        pass
    except Exception as exc:                                       # noqa: BLE001
        log.exception("worker died: %s", exc)
        return 1
    finally:
        lock.close()
    return 0

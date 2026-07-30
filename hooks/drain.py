#!/usr/bin/env python3
"""The drain hook: the guaranteed delivery path, and the finish gate's fast half.

Claims at most one advisory from the session mailbox and emits it in a single hook
response carrying **both** fields — `hookSpecificOutput.additionalContext` for the
model and `systemMessage` for the human, with identical text. That pairing is the
transparency invariant of the whole plugin (DESIGN.md §Say it to both): a channel
that cannot show the user what it told the agent does not qualify.

Three modes, one mechanism:

- `tool`   — beside the next tool result. No queue position, no turn of its own.
- `prompt` — alongside the user's next prompt; a natural boundary, and the moment
             the goal may have changed.
- `stop`   — the finish gate's immediate half: it may *continue* a finished turn,
             but only with an advisory already gated and waiting, above a higher
             floor, and at most once per task per hour.

Like the feed hook it exits 0 on every path, and on lock contention it emits
nothing at all — the other channel is by definition mid-delivery.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))

from second_brain import mailbox, paths, spool                    # noqa: E402
from second_brain.advice import Advisory                          # noqa: E402
from second_brain.config import Config                            # noqa: E402
from second_brain.projection import META, Observation             # noqa: E402

HOOK_EVENT = {"tool": "PostToolUse", "prompt": "UserPromptSubmit", "stop": "Stop"}


def _record_delivery(session_id: str, advisory: Advisory, channel: str) -> None:
    """Tell the worker an advisory landed, so it can open an outcome record.

    Delivery is observed the same way everything else is — through the spool —
    rather than by writing into the worker's state behind its back.
    """
    spool.append(session_id, [Observation(
        kind=META, ts=time.time(), body="delivered",
        meta={"event": "delivered", "advice_id": advisory.id, "kind": advisory.kind,
              "dedup_key": advisory.dedup_key, "channel": channel,
              "human_only": advisory.human_only},
    )])


def _claim_turn_report(session_id: str, max_age_s: float) -> str:
    """The last turn-end pass's verdicts, once, for the HUMAN only.

    The Stop hook returns milliseconds after a turn ends while the turn-end pass
    takes seconds, so the report is written by the worker when the pass completes
    and shown here at the next interaction. Claimed by unlink: exactly once,
    whichever drain gets there first. Never placed in the model's context —
    verdicts about the agent's turn are for the person supervising it.
    """
    path = paths.turn_report_path(session_id)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        path.unlink()
    except (OSError, ValueError):
        return ""
    if not isinstance(report, dict) or time.time() - float(report.get("at", 0)) > max_age_s:
        return ""
    lines = [str(line) for line in report.get("lines") or []]
    if not lines:
        return ""
    return ("🧠 Second Brain — turn-end verdicts (not shown to the agent):\n   "
            + "\n   ".join(lines))


def _finish_gate_budget(task_id: str, cfg: Config) -> tuple[bool, str]:
    """Whether the finish gate may fire for this task right now."""
    path = paths.finish_gate_path(task_id)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    now = time.time()
    interval = float(cfg.get("finish_gate.min_interval_s", 3600))
    cap = int(cfg.get("finish_gate.per_task_cap", 3))
    count = int(state.get("count", 0))
    last = float(state.get("last_s", 0.0))
    if count >= cap:
        return False, f"per-task cap of {cap} reached"
    if now - last < interval:
        return False, f"last intervention {int(now - last)}s ago, minimum {int(interval)}s"
    return True, ""


def _charge_finish_gate(task_id: str) -> None:
    path = paths.finish_gate_path(task_id)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    paths.write_private(path, json.dumps({
        "last_s": time.time(), "count": int(state.get("count", 0)) + 1,
    }))


def main(mode: str) -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0

    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return 0
    workspace = paths.workspace_key(str(payload.get("cwd") or "."))
    cfg = Config.load(workspace)
    if not cfg.observing(workspace):
        return 0

    queue_timeout = float(cfg.get("gate.queue_timeout_s", 1800))
    headline_cap = int(cfg.get("gate.headline_cap", 160))
    body_cap = int(cfg.get("gate.body_cap", 700))

    if mode == "stop":
        return _drain_stop(payload, session_id, cfg, queue_timeout, headline_cap, body_cap)

    report = _claim_turn_report(session_id, queue_timeout)
    advisory = mailbox.claim(session_id, queue_timeout_s=queue_timeout,
                             predicate=lambda a: not a.finish_gate)
    if advisory is None:
        if report:
            print(json.dumps({"systemMessage": report}))
        return 0

    _record_delivery(session_id, advisory, mode)
    if advisory.human_only:
        # Sub-threshold: the person hears about it, the model's context is untouched.
        text = advisory.for_user_only(headline_cap, body_cap)
        print(json.dumps({"systemMessage": text + ("\n\n" + report if report else "")}))
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": HOOK_EVENT.get(mode, "PostToolUse"),
            "additionalContext": advisory.for_agent(headline_cap, body_cap),
        },
        # The turn report rides the user-facing field ONLY — it is a human
        # surface, and the agent's copy above deliberately does not carry it.
        "systemMessage": advisory.for_user(headline_cap, body_cap)
        + ("\n\n" + report if report else ""),
    }))
    return 0


def _drain_stop(payload: dict[str, object], session_id: str, cfg: Config,
                queue_timeout: float, headline_cap: int, body_cap: int) -> int:
    """The finish gate: the one time this plugin may keep the agent working."""
    if not cfg.get("finish_gate.enabled", True):
        return 0
    if payload.get("stop_hook_active"):
        return 0        # we are already inside a continued turn; never chain

    floor = float(cfg.get("finish_gate.confidence_floor", 0.75))
    advisory = mailbox.claim(
        session_id, queue_timeout_s=queue_timeout,
        predicate=lambda a: a.finish_gate and not a.human_only and a.confidence >= floor,
    )
    if advisory is None:
        return 0

    allowed, why = _finish_gate_budget(advisory.task_id or session_id, cfg)
    if not allowed:
        # Budget refuses: the advisory is spent rather than requeued, because by the
        # next stop it would be answering a question the session has moved past.
        print(json.dumps({"systemMessage":
                          f"🧠 Second Brain held back a finish-gate advisory ({why})."}))
        return 0

    _charge_finish_gate(advisory.task_id or session_id)
    _record_delivery(session_id, advisory, "stop")
    print(json.dumps({
        "decision": "block",
        "reason": advisory.for_agent(headline_cap, body_cap),
        "systemMessage": advisory.for_user(headline_cap, body_cap)
        + "\n— this continued the turn; `/second-brain-config set finish_gate.enabled false` to stop that",
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "tool"))
    except BaseException:                                         # noqa: BLE001
        sys.exit(0)

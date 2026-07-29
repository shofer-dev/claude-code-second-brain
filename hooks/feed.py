#!/usr/bin/env python3
"""The feed hook: the primary's clock, and the only thing that reads its transcript.

Fires on every observable event, projects whatever the transcript has gained since
its last run, appends the result to the session spool, and exits 0 — with no
output, ever. It is stateless, sub-second and best-effort: if no worker is running
it still writes the spool and the session is untouched.

**Nothing here may fail a tool call.** Every path is wrapped so the hook exits 0
whatever happens (DESIGN.md decision #12, "fail open, always"). A crash in the
Second Brain must be invisible to the agent it is watching.

Usage: `feed.py <session_start|user_prompt|post_tool|subagent_stop|stop|session_end>`
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))

from second_brain import paths, spool, transcript                      # noqa: E402
from second_brain.config import Config                                 # noqa: E402
from second_brain.projection import META, SUBAGENT, Observation, cap, project_records  # noqa: E402
from second_brain.spawn import ensure_worker                           # noqa: E402


def _meta(event: str, ts: float, **fields: object) -> Observation:
    """A control record: read by the worker, never rendered into the window."""
    return Observation(kind=META, ts=ts, body=event, meta={"event": event, **fields})


def main(mode: str) -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0

    session_id = str(payload.get("session_id") or "")
    cwd = str(payload.get("cwd") or "")
    transcript_path = str(payload.get("transcript_path") or "")
    if not session_id:
        return 0

    # Enrolment is checked before anything is read: nothing about an unenrolled
    # project leaves the disk, and no worker starts (§Scope and consent).
    workspace = paths.workspace_key(cwd or ".")
    cfg = Config.load(workspace)
    if not cfg.observing(workspace):
        return 0

    now = time.time()
    projection_cfg = cfg.group("projection")
    # Control records that precede this event's transcript content, and those that
    # follow it. Order is load-bearing: a `stop` marker must come *after* the final
    # assistant message it ends, or the finish gate judges an incomplete turn.
    pre: list[Observation] = []
    post: list[Observation] = []
    records: list[Observation] = pre

    if mode == "session_start":
        source = str(payload.get("source") or "startup")
        # A brand-new session has no history worth projecting; a resumed one has
        # already been projected up to its saved offset.
        if source in {"startup", "clear"} and transcript_path:
            transcript.seek_to_end(transcript_path, session_id)
        pre.append(_meta("session_start", now, source=source, cwd=cwd, workspace=workspace))
    elif mode == "session_end":
        post.append(_meta("session_end", now, reason=str(payload.get("reason") or "")))
    elif mode == "subagent_stop":
        final = str(payload.get("last_assistant_message") or "")
        if final.strip():
            # The conclusion without the transcript: what the primary itself acts
            # on, at ~0.6 % of the volume of following the sidechain (§Subagents).
            post.append(Observation(
                kind=SUBAGENT, ts=now, raw_chars=len(final),
                tool=str(payload.get("agent_type") or "subagent"),
                body=cap(final, int(projection_cfg.get("subagent_final_cap", 1500)), projection_cfg),
                meta={"agent_id": str(payload.get("agent_id") or "")},
            ))
        post.append(_meta("subagent_stop", now, agent_id=str(payload.get("agent_id") or "")))
    elif mode == "stop":
        post.append(_meta("stop", now, stop_hook_active=bool(payload.get("stop_hook_active"))))
    elif mode == "user_prompt":
        pre.append(_meta("user_prompt", now))

    # The transcript is the content for every mode: the hook event says *when*,
    # the transcript says *what*, and only the transcript carries the assistant's
    # narration between tool calls.
    projected: list[Observation] = []
    if transcript_path:
        try:
            new_records = transcript.read_new_records(transcript_path, session_id)
            projected = project_records(new_records, projection_cfg)
        except (OSError, ValueError):
            pass

    records = [*pre, *projected, *post]
    if records:
        spool.append(session_id, records)

    if mode == "session_end":
        return 0
    ensure_worker(session_id, cwd, transcript_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "post_tool"))
    except BaseException:                                        # noqa: BLE001
        # Fail open, unconditionally: a broken observer must never break a session.
        sys.exit(0)

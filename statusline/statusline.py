#!/usr/bin/env python3
"""One line of Second Brain status, with no model in the loop.

Every slash command in Claude Code is a *prompt*: the `!` block runs, its output is
injected, and then the model is invoked to relay it. That is fine for a considered
read of `/second-brain-why`, and wrong for "is it watching, and what has it cost me?"
— a question you want answered continuously and for free. Asking a frontier model to
echo a status line is the most expensive way to render nine words.

The statusline is the answer: the harness runs this command itself and prints what
it returns. No turn, no tokens, no model. It reads the same status file the worker
writes through on every pass, so it is exactly as fresh as `/second-brain-stats`.

Wire it up in settings.json:

    "statusLine": {
      "type": "command",
      "command": "python3 ~/.claude/plugins/cache/shofer-second-brain/second-brain/0.1.0/statusline/statusline.py"
    }

Claude Code passes session context as JSON on stdin; the session id in it selects
the right worker when several sessions share a workspace. Absent that, the most
recently updated status for this working directory is used.

Prints nothing at all when no worker is watching — an empty segment is the honest
display for "not running", and it keeps the statusline out of the way in projects
where the plugin is not enrolled.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))

from second_brain import paths, pricing, status  # noqa: E402


def _read_context() -> dict[str, object]:
    """Claude Code hands the statusline command a JSON context on stdin."""
    try:
        if sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        return {}


def _select(context: dict[str, object]) -> dict[str, object] | None:
    session_id = str(context.get("session_id") or "")
    if session_id:
        found = status.read(session_id)
        if found:
            return found
    cwd = str((context.get("workspace") or {}).get("current_dir")
              if isinstance(context.get("workspace"), dict) else context.get("cwd") or "") or "."
    workspace = paths.workspace_key(cwd)
    for record in status.read_all(max_age_s=3600):
        if record.get("workspace") == workspace:
            return record
    return None


def render(record: dict[str, object]) -> str:
    """`🧠 watching · 3 passes · $0.04` — short enough to share the line."""
    state = str(record.get("state", "?"))
    if state.startswith("silent"):
        glyph, label = "🧠", "silent (budget)"
    elif state == "muted":
        glyph, label = "🔇", "muted"
    elif state == "thinking":
        glyph, label = "🧠", "thinking"
    elif state == "stopped":
        return ""                       # the session's worker is gone; say nothing
    else:
        glyph, label = "🧠", "watching"

    parts = [f"{glyph} {label}"]
    passes = int(record.get("passes", 0) or 0)
    if passes:
        parts.append(f"{passes} pass{'es' if passes != 1 else ''}")

    delivered = int(record.get("advisories_delivered", 0) or 0)
    if delivered:
        parts.append(f"{delivered} advisor{'ies' if delivered != 1 else 'y'}")

    # The last turn-end verdict, fresh: the one user-visible surface that needs
    # no next interaction, so the person who just watched the turn end sees the
    # outcome of its pass without typing anything.
    verdict = str(record.get("turn_verdict") or "")
    verdict_at = float(record.get("turn_verdict_at", 0) or 0)
    if verdict and time.time() - verdict_at < 900:
        parts.append(f"last turn: {verdict}")

    tokens = record.get("tokens") or {}
    if isinstance(tokens, dict) and any(tokens.values()):
        cost = pricing.estimate(str(record.get("model", "")),
                                {k: int(v or 0) for k, v in tokens.items()})
        if cost.known and cost.total >= 0.0001:
            parts.append(f"${cost.total:.2f}")

    stale = time.time() - float(record.get("updated_at", 0) or 0)
    if stale > 300:
        parts.append("stale")
    return " · ".join(parts)


def main() -> int:
    record = _select(_read_context())
    if record is None:
        return 0                        # not watching here: print nothing
    line = render(record)
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                                          # noqa: BLE001
        # A statusline that raises would print a traceback into the user's prompt.
        sys.exit(0)

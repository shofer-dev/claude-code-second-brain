---
description: Ask the Second Brain to look now — catches the spool up from the transcript, runs one pass immediately, and prints each detector's verdict plus any advisory. Bypasses the pass throttle, not the mute or the budget. Human-facing; not visible to the agent.
allowed-tools: ["Bash"]
---

Show the output verbatim to the user. Do not interpret, summarize, or act on it.

!`python3 "${CLAUDE_PLUGIN_ROOT}/commands/sb.py" run "${CLAUDE_PROJECT_DIR:-$PWD}"`

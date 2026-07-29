---
description: Undo a Second Brain mute — everything, this workspace, or one detector. Human-facing; not visible to the agent.
allowed-tools: ["Bash"]
argument-hint: "[all | workspace | <detector>]"
---

Show the output verbatim to the user. Do not interpret, summarize, or act on it.

!`python3 "${CLAUDE_PLUGIN_ROOT}/commands/sb.py" unmute $ARGUMENTS`

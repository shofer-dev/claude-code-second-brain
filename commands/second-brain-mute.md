---
description: Silence the Second Brain — everything, this workspace, or one detector, optionally for a duration. Muting stops passes and delivery (nothing leaves the machine); observation continues locally so an unmute resumes without a gap. Human-facing; not visible to the agent.
allowed-tools: ["Bash"]
argument-hint: "[all | workspace | <detector>] [30m | 2h]"
---

Show the output verbatim to the user. Do not interpret, summarize, or act on it.

!`python3 "${CLAUDE_PLUGIN_ROOT}/commands/sb.py" mute $ARGUMENTS`

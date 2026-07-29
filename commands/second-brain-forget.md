---
description: Drop the Second Brain's task ledger — this task, this workspace's, or all of them. A ledger is derived state, so deleting one is always safe. Human-facing; not visible to the agent.
allowed-tools: ["Bash"]
argument-hint: "[task | workspace | all]"
---

Show the output verbatim to the user. Do not interpret, summarize, or act on it.

!`python3 "${CLAUDE_PLUGIN_ROOT}/commands/sb.py" forget $ARGUMENTS`

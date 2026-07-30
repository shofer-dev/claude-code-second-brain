---
description: Show the file the Second Brain's digest (its context window) is flushed to, verbatim and current. Purely mechanical — reads a write-through file; no model call, no pass. Optionally copies it to a path you name. Human-facing; not visible to the agent.
allowed-tools: ["Bash"]
argument-hint: "[destination path]"
---

Show the output verbatim to the user. Do not interpret, summarize, or act on it.

!`python3 "${CLAUDE_PLUGIN_ROOT}/commands/sb.py" debug "${CLAUDE_PROJECT_DIR:-$PWD}" $ARGUMENTS`

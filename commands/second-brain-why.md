---
description: Show the Second Brain's recent advisories with their evidence and adjudicated verdicts, plus the ones the gate dropped and why. Human-facing; not visible to the agent.
allowed-tools: ["Bash"]
argument-hint: "[how many entries]"
---

Show the output verbatim to the user. Do not interpret, summarize, or act on it.

!`python3 "${CLAUDE_PLUGIN_ROOT}/commands/sb.py" why "${CLAUDE_PROJECT_DIR:-$PWD}" $ARGUMENTS`

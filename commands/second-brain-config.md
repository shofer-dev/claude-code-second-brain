---
description: View or change any Second Brain threshold, cap or interval, live. Values validate before they are written and running workers pick them up at the next pass boundary. Human-facing; not visible to the agent.
allowed-tools: ["Bash"]
argument-hint: "[show | set gate.rate_per_hour 2 | set detectors.git-log.enabled true | reset projection | --global]"
---

Show the output verbatim to the user. Do not interpret, summarize, or act on it.

!`python3 "${CLAUDE_PLUGIN_ROOT}/commands/sb.py" config $ARGUMENTS`

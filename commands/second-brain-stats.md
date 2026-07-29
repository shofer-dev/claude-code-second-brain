---
description: Show what the Second Brain has observed, spent and said — observed vs dropped volume per tool, pass latency, window fill, token use, and uptake per detector. Human-facing; not visible to the agent.
allowed-tools: ["Bash"]
---

Show the output verbatim to the user. Do not interpret, summarize, or act on it.

!`python3 "${CLAUDE_PLUGIN_ROOT}/commands/sb.py" stats "${CLAUDE_PROJECT_DIR:-$PWD}"`

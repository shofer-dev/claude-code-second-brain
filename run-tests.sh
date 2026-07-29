#!/usr/bin/env bash
# The gate: everything must pass before a commit.
#
# The suite is fully offline — no provider, no network, no API key. `conftest.py`
# refuses both `make_provider` and the HTTP transport, so a test that reaches for a
# model fails instead of quietly spending a real subscription's budget.
set -euo pipefail
cd "$(dirname "$0")"

echo "── compiling ────────────────────────────────────────────"
python3 -m compileall -q worker hooks commands

echo "── tests ────────────────────────────────────────────────"
python3 -m pytest tests/ -q "$@"

echo "── manifests ────────────────────────────────────────────"
python3 - <<'PY'
import json, pathlib, sys
for path in [".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
             "hooks/hooks.json", "monitors/monitors.json"]:
    try:
        json.loads(pathlib.Path(path).read_text())
    except Exception as exc:
        sys.exit(f"✗ {path}: {exc}")
    print(f"✓ {path}")
PY

echo
echo "green."

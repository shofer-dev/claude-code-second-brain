#!/usr/bin/env python3
"""Entry point for the session worker — the plugin monitor's command.

Kept to nothing but a path fix and a call, so the same module runs whether Claude
Code started it as a monitor or a hook spawned it detached.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from second_brain.worker import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

"""What a "task" is, concretely — because no hook payload carries one.

The ledger is scoped to the task, so the worker has to derive task identity itself.
`SessionStart`'s `source` gives the hard signals; the soft one is a user prompt the
observer judges to be a new goal, which matters because a single session routinely
carries three unrelated pieces of work and carrying task 1's decisions into task 3's
advice is exactly the drift that task scoping exists to prevent.

The soft rule is deliberately allowed to be wrong: **a false split costs a cold
start, a missed split costs some irrelevant prefix.** Neither is a correctness
failure, which is why a cheap heuristic is the right answer here and a model call
per prompt would not be.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field

from . import paths
from .ledger import Ledger


def mint() -> str:
    return f"t-{uuid.uuid4().hex[:8]}"


@dataclass
class Binding:
    """Which task a session is currently working on."""

    session_id: str
    task_id: str = ""
    workspace: str = ""
    cwd: str = ""
    epoch: int = 0
    started_at: float = field(default_factory=time.time)

    @classmethod
    def load(cls, session_id: str) -> Binding:
        try:
            data = json.loads(paths.session_state_path(session_id).read_text(encoding="utf-8"))
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            binding = cls(**known)
            binding.session_id = session_id
            return binding
        except (OSError, ValueError, TypeError):
            return cls(session_id=session_id)

    def save(self) -> None:
        paths.write_private(paths.session_state_path(self.session_id),
                            json.dumps(asdict(self)))


def on_session_start(binding: Binding, source: str, *, workspace: str, cwd: str) -> tuple[Binding, str]:
    """Apply the hard signals. Returns the binding and a one-line note for the log."""
    binding.workspace = workspace or binding.workspace
    binding.cwd = cwd or binding.cwd

    if source in {"startup", "clear"} or not binding.task_id:
        binding.task_id = mint()
        binding.epoch = 0
        binding.started_at = time.time()
        note = "new task (%s)" % source
    elif source == "fork":
        # Both sides continue independently from a common history.
        parent = Ledger.load(binding.task_id, workspace)
        binding.task_id = mint()
        parent.fork_to(binding.task_id)
        note = "forked task"
    else:
        note = f"continuing task ({source})"
    binding.save()
    return binding, note


# A prompt that starts a new piece of work usually says so. This is the cheap
# structural proxy the design asks for: no model call, and wrong in the cheap
# direction (an extra split) more often than in the expensive one.
_NEW_GOAL_PREFIXES = (
    "now ", "next, ", "next ", "new task", "let's ", "lets ", "forget ", "instead",
    "switch to", "different ", "unrelated", "one more thing", "another thing",
)
_CONTINUATION_MARKERS = (
    "also", "and ", "but ", "that ", "it ", "this ", "why", "keep going", "continue",
    "fix that", "same", "again", "no,", "yes", "ok", "thanks",
)


def looks_like_new_goal(prompt: str, since_task_start_s: float) -> bool:
    """Whether a user prompt reads as a new goal rather than a continuation."""
    text = prompt.strip().lower()
    if len(text) < 25 or since_task_start_s < 300:
        return False        # short prompts are follow-ups; young tasks are not restarts
    if any(text.startswith(marker) for marker in _CONTINUATION_MARKERS):
        return False
    return any(marker in text[:80] for marker in _NEW_GOAL_PREFIXES)


def new_epoch(binding: Binding, reason: str) -> Binding:
    """Start a fresh task within the same session (a soft, model-free split)."""
    binding.task_id = mint()
    binding.epoch += 1
    binding.started_at = time.time()
    binding.save()
    return binding

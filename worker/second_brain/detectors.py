"""The detector contract, and the catalogue that ships against it.

A detector is **its own system prompt + its own tool set + an output schema + a
trigger** (DESIGN.md §The contract). Adding one is a config entry, never a code
path: there is one fork implementation, instantiated N times and parameterised by
what is in this file.

Two things about the system prompt are worth stating where the prompts live.
It really is the detector's own — but it is sent **after the cache breakpoint**,
as a second system block, never ahead of the shared window. Put it first and every
fork's prefix differs, every fork pays full price, and the fan-out's economics
disappear. Functionally its own prompt; structurally the suffix of one.

Enablement follows the shipping order rather than the size of the catalogue: v1
turns on the open-ended `default` plus the two no-tool detectors that prove the
contract. Everything that reads the repo, walks history or *executes* something
ships defined and **off**, one `/second-brain-config` away.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .constants import FEEDBACK_TOOL
from .tools import ToolGrant, parse_grants

# ── the one tool every fork ends by calling ─────────────────────────────────
FEEDBACK_SCHEMA: dict[str, Any] = {
    "name": FEEDBACK_TOOL,
    "description": (
        "Report this detector's answer for this pass. Call this exactly once, and call "
        "it even when you have nothing to say — `silent` is the expected steady state."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["silent", "advise", "resolved", "still_open"],
                "description": ("silent: nothing worth the agent's attention. advise: a specific, "
                                "evidenced finding. resolved: a finding you raised earlier is now "
                                "handled. still_open: it stands but is not worth re-sending."),
            },
            "headline": {"type": "string", "description": "One line. Often all that gets read."},
            "body": {"type": "string", "description": "The argument, and what to do about it."},
            "evidence": {
                "type": "array", "items": {"type": "string"},
                "description": ("Specific observations, file:line locators or commit shas. An advisory "
                                "without evidence is dropped."),
            },
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "dedup_key": {
                "type": "string",
                "description": ("Stable semantic key, e.g. 'tests-not-run:services/foo'. The same "
                                "finding must produce the same key across passes."),
            },
            "stale_if": {
                "type": "array", "items": {"type": "string"},
                "description": ("Regular expressions over later observations that would make this "
                                "advice already-handled, e.g. 'go test'. Checked before delivery."),
            },
            "finish_gate": {
                "type": "boolean",
                "description": ("True only for evidenced unfinished work worth continuing a turn the "
                                "agent believes is over."),
            },
            "outcomes": {
                "type": "array",
                "description": "Verdicts on this detector's own outstanding advisories.",
                "items": {
                    "type": "object",
                    "properties": {
                        "advice_id": {"type": "string"},
                        "verdict": {
                            "type": "string",
                            "enum": ["adopted", "partially_adopted", "rejected",
                                     "already_handled", "no_evidence", "contradicted"],
                        },
                        "evidence": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["advice_id", "verdict"],
                },
            },
        },
        "required": ["verdict"],
    },
}

OUTCOME_VERDICTS = ("adopted", "partially_adopted", "rejected",
                    "already_handled", "no_evidence", "contradicted")

# ── the catalogue ───────────────────────────────────────────────────────────
BUILTIN_DETECTORS: dict[str, dict[str, Any]] = {
    "repeat-failure": {
        "enabled": True,
        "pilot": True,          # cheapest and most predictable: it warms the prefix
        "system": (
            "You watch for a session burning time on a loop it cannot see.\n"
            "Advise only when the SAME command or approach has failed three or more times with "
            "cosmetic variations, or when the agent is retrying something that already failed the "
            "same way. Cite the failing observations. Everything else is silence."
        ),
        "tools": [],
        "cadence": "every_pass",
        "confidence_floor": 0.6,
    },
    "standard-questions": {
        "enabled": True,
        "system": (
            "You are checking whether the work has answered a fixed set of questions.\n"
            "Ask only about a question the observation stream does not already answer — if the agent "
            "ran the tests, the question is answered and you say nothing. If the task looks close to "
            "done and nothing in the stream ever answers one, that silence is the finding.\n"
            "You cannot see tool results, only what was run, so ask whether an ACTION occurred — "
            "never whether it succeeded."
        ),
        "tools": [],
        "cadence": "every_pass",
        "confidence_floor": 0.6,
        "config": {
            "questions": [
                {"key": "tests_run", "ask": "Were the tests run since the first edit?"},
                {"key": "tests_added", "ask": "Were tests added for new code paths?"},
                {"key": "compiles", "ask": "Was a build or type-check run since the edits?"},
                {"key": "deployed", "ask": "If a version was bumped, was a deploy command run?"},
                {"key": "docs_updated", "ask": "Was the neighbouring doc updated with the code?"},
            ],
        },
    },
    "default": {
        "enabled": True,
        "system": (
            "You are watching, not participating.\n"
            "Speak only when you have something genuinely useful: a mistake about to compound, a "
            "constraint stated earlier and now contradicted, prior art the agent should see, a "
            "decision whose cost will only become visible later.\n"
            "The expected steady state is silence. Do not summarise, do not encourage, do not "
            "restate what the agent just said, and never advise something it has already done."
        ),
        "tools": ["Read", "Grep", "Glob"],
        "cadence": "every_pass",
        "confidence_floor": 0.65,
    },
    "goal-drift": {
        "enabled": False,
        "system": (
            "You watch for the work drifting off the goal.\n"
            "The user asked for A; several turns later the work is about B, with no acknowledgment "
            "that the goal changed. Advise only with both halves cited: what was asked, and what is "
            "now being done. A user who redirected mid-task is not drift."
        ),
        "tools": [],
        "cadence": "every_nth:2",
        "confidence_floor": 0.7,
    },
    "git-log": {
        "enabled": False,
        "system": (
            "You check whether the area being edited was changed recently, and whether that history "
            "contradicts what is being done now.\n"
            "Use the git commands you have on the files in the observation stream. The finding worth "
            "sending is specific: 'this was rewritten two days ago in <sha>; the thing being re-added "
            "was deliberately removed'. A file simply having history is not a finding."
        ),
        "tools": ["Read", {"exec": [
            "git log --oneline -20",
            "git log --oneline -20 --stat",
            "git status --short",
        ]}],
        "cadence": "every_nth:2",
        "confidence_floor": 0.65,
    },
    "prior-art": {
        "enabled": False,
        "system": (
            "You check whether what is being built already exists in this repository.\n"
            "Search for an existing helper, shared library or sibling implementation before the agent "
            "finishes rebuilding it. Cite the path. Silence unless you have actually found the thing."
        ),
        "tools": ["Read", "Grep", "Glob"],
        "cadence": "every_nth:3",
        "confidence_floor": 0.7,
    },
    "constraint-drift": {
        "enabled": False,
        "system": (
            "You check the work against the rules this project writes down — CLAUDE.md, AGENTS.md, "
            "and constraints the user stated earlier in the task.\n"
            "Quote the rule and the observation that contradicts it. This detector is the most "
            "false-positive-prone one there is: if you are inferring a rule rather than reading one, "
            "stay silent."
        ),
        "tools": ["Read", "Grep", "Glob"],
        "cadence": "every_nth:3",
        "confidence_floor": 0.75,
    },
    "static-analysis": {
        "enabled": False,       # opt-in: it executes
        "system": (
            "You determine whether the tree the agent just edited still builds and type-checks.\n"
            "Run the command you are allowed to run, once, and report only a real failure with the "
            "compiler's own first error line as evidence. A build you could not run is silence, "
            "not a finding."
        ),
        "tools": [{"exec": []}],   # the workspace supplies its own build command
        "cadence": "every_nth:4",
        "deadline_s": 45,
        "confidence_floor": 0.8,
    },
    "cross-task-collision": {
        "enabled": False,
        "system": (
            "Another live session is editing files this task is also editing. The collision itself is "
            "already established — you are not asked to judge whether it is real, only to write one "
            "clear line about it.\n"
            "Say which file, which other task, and which case it is: the same checkout (both agents "
            "write the same file, later writer wins silently — urgent) or separate worktrees (two "
            "branches diverging, git will report it at merge time — lower)."
        ),
        "tools": [],
        "cadence": "every_pass",
        "confidence_floor": 0.6,
        "structural": True,     # fires on a match the worker computes, not a judgment
    },
}


@dataclass
class Detector:
    """One resolved lens: its prompt, its reach, its budget and its trigger."""

    name: str
    system: str
    grant: ToolGrant
    enabled: bool = True
    cadence: str = "every_pass"
    confidence_floor: float = 0.6
    deadline_s: float = 0.0
    pilot: bool = False
    structural: bool = False
    config: dict[str, Any] = field(default_factory=dict)

    def due(self, pass_number: int, stride: int = 1) -> bool:
        """Whether this detector runs on this pass, honouring any demotion stride."""
        every = 1
        if self.cadence.startswith("every_nth"):
            _, _, raw = self.cadence.partition(":")
            try:
                every = max(1, int(raw))
            except ValueError:
                every = 1
        every = max(every, max(1, stride))
        return pass_number % every == 0

    def suffix(self) -> str:
        """This detector's instructions — the head of its fork's private tail.

        Never a system block and never ahead of the window: anything per-fork in
        `tools` or `system` sits before the message-level cache breakpoint and
        would give every fork a different prefix (see fork.py's module docstring).
        """
        extra = ""
        questions = self.config.get("questions")
        if questions:
            rendered = "\n".join(
                f"- {q['ask']}" if isinstance(q, dict) else f"- {q}" for q in questions
            )
            extra = f"\n\nThe questions you hold against this task:\n{rendered}"
        return f"You are the '{self.name}' detector.\n\n{self.system}{extra}"


def resolve(cfg_detectors: dict[str, Any]) -> list[Detector]:
    """Build the detector list from merged configuration, in a stable order."""
    out: list[Detector] = []
    for name in sorted(cfg_detectors):
        spec = cfg_detectors[name]
        if not isinstance(spec, dict):
            continue
        out.append(Detector(
            name=name,
            system=str(spec.get("system", "")),
            grant=parse_grants(spec.get("tools")),
            enabled=bool(spec.get("enabled", False)),
            cadence=str(spec.get("cadence", "every_pass")),
            confidence_floor=float(spec.get("confidence_floor", 0.6)),
            deadline_s=float(spec.get("deadline_s", 0.0) or 0.0),
            pilot=bool(spec.get("pilot", False)),
            structural=bool(spec.get("structural", False)),
            config=dict(spec.get("config") or {}),
        ))
    return out


def enabled(detectors: list[Detector]) -> list[Detector]:
    return [d for d in detectors if d.enabled and d.system.strip()]


def pick_pilot(detectors: list[Detector]) -> Detector | None:
    """The fork that runs first, alone, to write the shared prefix into the cache.

    Preference order: whatever declares itself the pilot, then the cheapest
    tool-less detector — never one that might spend its deadline inside a build,
    because everything else waits on it.
    """
    for detector in detectors:
        if detector.pilot:
            return detector
    for detector in detectors:
        if detector.grant.empty:
            return detector
    return detectors[0] if detectors else None

"""The detector contract, and the catalogue that ships against it.

A detector is **its own system prompt + its own tool set + an output schema + a
trigger** (DESIGN.md §The contract). Adding one is a catalogue entry, never a code
path: there is one fork implementation, instantiated N times and parameterised by
what `load_catalogue` returns — the bundled `detectors.json` beside this module,
or the file `catalogue.file` points at.

One thing about the system prompt is worth stating where the contract lives: it
really is the detector's own — but it rides the fork's private message tail,
after the cache breakpoint, never a system block and never ahead of the shared
window. Put it earlier and every fork's prefix differs, every fork pays full
price, and the fan-out's economics disappear (see fork.py's module docstring).

Enablement follows the shipping order rather than the size of the catalogue: v1
turns on the open-ended `default` plus the two no-tool detectors that prove the
contract. Everything that reads the repo, walks history or *executes* something
ships defined and **off**, one `/second-brain-config` away.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
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
_BUNDLED_CATALOGUE = Path(__file__).with_name("detectors.json")


def load_catalogue(path: str | None = None) -> dict[str, dict[str, Any]]:
    """The detector catalogue — the source of truth is a JSON file people edit.

    The bundled `detectors.json` beside this module ships the built-ins;
    `catalogue.file` points at a user's own file, whose entries **replace** the
    bundle (copy the bundled file to extend it — replacement is predictable,
    merging resurrects detectors you meant to be rid of). Config overrides
    (`detectors.<name>.<field>`, global and workspace) still merge on top of
    whichever catalogue is in force. Any failure to read or parse falls back to
    the bundle: a broken catalogue must degrade to the shipped one, never to no
    observer. Re-read on every load, so edits take effect at the next pass
    boundary like every other configuration change.
    """
    candidates = ([Path(path).expanduser()] if path else []) + [_BUNDLED_CATALOGUE]
    for candidate in candidates:
        try:
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(loaded, dict) and loaded
                and all(isinstance(v, dict) for v in loaded.values())):
            return loaded
    return {}


BUILTIN_DETECTORS: dict[str, dict[str, Any]] = load_catalogue()
"""The bundled catalogue, as loaded at import — kept for introspection; the
config layer calls `load_catalogue()` itself so `catalogue.file` can differ."""


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

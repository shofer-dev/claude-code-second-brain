"""Between "the model produced an advisory" and "the primary sees it".

Generation is the easy part; **the gate is where this design earns its keep**,
because an asynchronous advisor has a failure mode a synchronous one does not: by
the time the advice arrives, the agent may have already done it, already decided
against it, or moved on. Most generated advice should die here.

The order is deliberate and each step drops a different kind of wrong (DESIGN.md
§Advice: generation → gating → delivery):

1. **evidence** — an advisory with no cited observation is not checkable, so it is
   not sent to anyone;
2. **confidence** — above the floor it may reach the agent; between the two floors
   it reaches the *human only*, which costs the agent nothing;
3. **suppression and de-duplication** — including against advice that was *dropped*,
   or every pass burns the rate limit rediscovering the same finding;
4. **mute** — the user's explicit "not this, not now";
5. **rate limit and cooldown** — the primary's attention budget, which is a
   different budget from the observer's token spend and is configured separately;
6. **staleness** — re-checked again at delivery time, because the primary keeps
   working while an advisory sits in the mailbox.

Every decision is written to the advice history, including the drops, because a
gate whose reasoning is invisible is a gate nobody trusts (`/second-brain-why`).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import mailbox, paths
from .advice import Advisory
from .config import Config
from .fork import Feedback
from .ledger import Ledger
from .projection import Observation

_WORD = re.compile(r"[a-z0-9_./-]+")


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2}


def similarity(a: str, b: str) -> float:
    """Jaccard over word sets — cheap, deterministic, and good enough for dedup."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class Decision:
    """What the gate did with one candidate, and why."""

    detector: str
    headline: str
    outcome: str                      # delivered | human_only | dropped
    reason: str = ""
    advice_id: str = ""
    confidence: float = 0.0
    at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps({
            "at": round(self.at, 3), "detector": self.detector, "headline": self.headline,
            "outcome": self.outcome, "reason": self.reason, "advice_id": self.advice_id,
            "confidence": round(self.confidence, 3),
        }, ensure_ascii=False)


class Mute:
    """The mute flag, re-read from live configuration rather than cached.

    `mute.all` at the workspace layer silences one workspace; at the global
    layer, everything. A single detector is muted by disabling it
    (`detectors.<name>.enabled false`), which stops its forks entirely — so
    there is no per-detector check here. Muting stops passes and delivery;
    observation continues locally, so an unmute resumes without a gap.
    """

    def __init__(self, task_id: str, workspace: str) -> None:
        self.task_id = task_id
        self.workspace = workspace

    def observing(self) -> bool:
        """Re-loads the config on every call so a mute flip takes effect within
        one tick, not one pass — a muted worker runs no passes, so a
        pass-boundary reload would never see the unmute."""
        try:
            return not bool(Config.load(self.workspace).get("mute.all", False))
        except Exception:                                          # noqa: BLE001
            # Fail open: a broken config file must not silence the observer.
            return True


class Gate:
    """One task's gate. Holds the rate-limit and dedup state a pass needs."""

    def __init__(self, cfg: Config, ledger: Ledger, *, task_id: str, session_id: str,
                 workspace: str, log: Any = None) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.task_id = task_id
        self.session_id = session_id
        self.workspace = workspace
        self.log = log
        self.mute = Mute(task_id, workspace)
        self.delivered_at: list[float] = []
        self.seen: list[tuple[str, str]] = []      # (dedup_key, headline), sent or dropped
        self.decisions: list[Decision] = []

    # ── the pipeline ────────────────────────────────────────────────────────
    def consider(self, feedback: Feedback) -> Advisory | None:
        """Run one detector's `advise` through the gate. Returns what to enqueue."""
        if feedback.verdict != "advise" or not feedback.headline.strip():
            return None

        floor = float(self.cfg.get("gate.confidence_floor", 0.6))
        human_floor = float(self.cfg.get("gate.human_floor", 0.35))
        detector_floor = self._detector_floor(feedback.detector, floor)

        if not feedback.evidence:
            return self._drop(feedback, "no evidence cited")

        # No mute check here: `mute.all` stops the pass before any fork runs,
        # and a muted single detector (`detectors.<name>.enabled false`) never
        # forks — so nothing muted can reach the gate.
        key = feedback.dedup_key or feedback.headline[:60]
        if key in self.ledger.suppressed:
            return self._drop(feedback, f"suppressed ({self.ledger.suppressed[key]})")

        threshold = float(self.cfg.get("gate.dedup_threshold", 0.6))
        for seen_key, seen_headline in self.seen:
            if seen_key and seen_key == key:
                return self._drop(feedback, f"duplicate of {seen_key}")
            if similarity(seen_headline, feedback.headline) >= threshold:
                return self._drop(feedback, "near-duplicate of an earlier finding")
        for record in self.ledger.advised:
            if record.get("dedup_key") and record["dedup_key"] == key:
                return self._drop(feedback, "already advised this task")

        if feedback.confidence < human_floor:
            return self._drop(feedback, f"confidence {feedback.confidence:.2f} below the human floor")

        human_only = feedback.confidence < detector_floor
        if not human_only and not self._within_rate_limit():
            # Sub-threshold advice still reaches the human for free, so a rate-limited
            # finding is downgraded rather than lost.
            human_only = True
            self._note(feedback, "rate limited → user only")

        advisory = Advisory(
            task_id=self.task_id, session_id=self.session_id, workspace=self.workspace,
            kind=feedback.detector, headline=feedback.headline.strip(),
            body=feedback.body.strip(), confidence=feedback.confidence,
            evidence=feedback.evidence, dedup_key=key, stale_if=feedback.stale_if,
            ttl_s=float(self.cfg.get("gate.advice_ttl_s", 900)),
            human_only=human_only,
            finish_gate=feedback.finish_gate and feedback.confidence >= float(
                self.cfg.get("finish_gate.confidence_floor", 0.75)),
        )
        self.seen.append((key, advisory.headline))
        if not human_only:
            self.delivered_at.append(time.time())
        self._record(Decision(feedback.detector, advisory.headline,
                              "human_only" if human_only else "delivered",
                              "below the agent floor" if human_only else "",
                              advisory.id, feedback.confidence))
        return advisory

    def enqueue(self, advisory: Advisory) -> None:
        """Put a gated advisory in the mailbox and open its outcome record."""
        mailbox.put(self.session_id, advisory,
                    max_entries=int(self.cfg.get("gate.max_mailbox_entries", 8)),
                    queue_timeout_s=float(self.cfg.get("gate.queue_timeout_s", 1800)))
        if not advisory.human_only:
            self.ledger.record_advice(advisory.id, advisory.kind, advisory.headline,
                                      advisory.dedup_key)

    # ── staleness, re-checked while an advisory waits ───────────────────────
    def revalidate(self, observations: list[Observation]) -> int:
        """Drop queued advice the primary has since acted on. Returns how many.

        Mandatory, not an optimization: between generation and drain the primary
        keeps working, and an async advisor that tells the agent to do things it
        just finished is the fastest possible way to teach it to ignore the channel.
        """
        pending = mailbox.peek(self.session_id)
        if not pending or not observations:
            return 0
        text = "\n".join(o.body for o in observations)
        stale: set[str] = set()
        for advisory in pending:
            for pattern in advisory.stale_if:
                try:
                    if re.search(pattern, text, re.IGNORECASE):
                        stale.add(advisory.dedup_key or advisory.id)
                        self._record(Decision(advisory.kind, advisory.headline, "dropped",
                                              f"already handled (matched {pattern!r})",
                                              advisory.id, advisory.confidence))
                        break
                except re.error:
                    continue
        return mailbox.drop_keys(self.session_id, stale) if stale else 0

    def resolve(self, keys: set[str]) -> int:
        """A detector reported its own finding resolved: pull it before it lands."""
        if not keys:
            return 0
        return mailbox.drop_keys(self.session_id, keys)

    def expire_sweep(self) -> None:
        """Record anything the two delivery clocks caught, then forget it."""
        mailbox.claim(
            self.session_id,
            queue_timeout_s=float(self.cfg.get("gate.queue_timeout_s", 1800)),
            predicate=lambda _a: False,          # claim nothing; run the expiry pass
            on_expire=lambda advisory, clock: self._record(Decision(
                advisory.kind, advisory.headline, "dropped", f"expired ({clock})",
                advisory.id, advisory.confidence)),
        )

    # ── internals ───────────────────────────────────────────────────────────
    def _detector_floor(self, detector: str, default: float) -> float:
        """Calibration: a detector whose advice is ignored has to clear a higher bar."""
        uptake = self.ledger.uptake(detector)
        if uptake is None:
            return default
        if uptake < 0.2:
            return min(0.95, default + 0.2)
        if uptake > 0.7:
            return max(0.2, default - 0.1)
        return default

    def _within_rate_limit(self) -> bool:
        now = time.time()
        self.delivered_at = [t for t in self.delivered_at if now - t < 3600]
        if len(self.delivered_at) >= int(self.cfg.get("gate.rate_per_hour", 4)):
            return False
        cooldown = float(self.cfg.get("gate.cooldown_s", 300))
        return not (self.delivered_at and now - self.delivered_at[-1] < cooldown)

    def _drop(self, feedback: Feedback, reason: str) -> None:
        key = feedback.dedup_key or feedback.headline[:60]
        # Dropped advice is remembered too, or every pass regenerates the same
        # observation and burns the rate limit rediscovering it.
        self.seen.append((key, feedback.headline))
        self._record(Decision(feedback.detector, feedback.headline, "dropped", reason,
                              confidence=feedback.confidence))
        return None

    def _note(self, feedback: Feedback, reason: str) -> None:
        if self.log:
            self.log.info("gate: %s — %s", feedback.detector, reason)

    def _record(self, decision: Decision) -> None:
        self.decisions.append(decision)
        try:
            paths.append_private(paths.history_path(self.task_id), decision.to_json())
        except OSError:
            pass
        if self.log:
            self.log.info("gate: %s %s (%s)", decision.outcome, decision.detector, decision.reason)


def read_history(task_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """The last decisions, newest first — the data behind `/second-brain-why`."""
    try:
        lines = paths.history_path(task_id).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
        if len(out) >= limit:
            break
    return out

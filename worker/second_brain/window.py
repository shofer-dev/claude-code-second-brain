"""Window B — the observer's own conversation, and the rules that keep it cheap.

The window is **append-only between compactions**. That single property is the
difference between an observer that is affordable to run continuously and one that
is not: every pass re-reads a prefix the provider has already cached, so a pass
costs a cached read plus the new observations rather than a fresh full-price read
of everything seen so far.

The invariants are written as rules in DESIGN.md §Window discipline because each
has a tempting violation, and they are enforced here:

- **Nothing already in the window is rewritten, reordered or re-rendered.** The
  only writes are appends at the end. `_blocks` is never indexed for assignment.
- **New knowledge is never folded back into the prefix between compactions.**
  Detector feedback and advice outcomes are appended as ordinary blocks; they
  reach the ledger only at the next compaction.
- **Volatile content lives only in the trailing per-fork block**, which this class
  never holds — a fork appends it to its own private copy.
- **Compaction is the one sanctioned prefix rebuild**, amortized with hysteresis:
  triggered at a high-water mark, compacted down to a *floor*. Compacting back to
  the trigger would re-compact on the very next observation and thrash the cache
  on every pass.

Physically the prefix is one user message with many text blocks, which is what lets
a fork append its own block after the cache mark without touching anything shared.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .config import Config
from .constants import CHARS_PER_TOKEN
from .ledger import Ledger
from .projection import META, Observation
from .prompts import COMPACTION_SYSTEM, SHARED_SYSTEM, workspace_block


@dataclass(frozen=True)
class Snapshot:
    """An immutable view of the shared prefix, as handed to every fork of a pass."""

    system: tuple[dict[str, Any], ...]
    blocks: tuple[dict[str, Any], ...]

    def messages_for(self, fork_block: dict[str, Any]) -> list[dict[str, Any]]:
        """One user message: the shared blocks, then this fork's private tail."""
        return [{"role": "user", "content": [*self.blocks, fork_block]}]

    @property
    def cache_marks(self) -> list[tuple[int, int]]:
        """Mark the last shared block — everything after it differs per fork."""
        return [(0, len(self.blocks) - 1)] if self.blocks else []


def _text(block: str) -> dict[str, Any]:
    return {"type": "text", "text": block}


class Window:
    """Window B for one task."""

    def __init__(self, cfg: Config, ledger: Ledger, cwd: str) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.cwd = cwd
        self._system = [_text(SHARED_SYSTEM + "\n\n"
                              + workspace_block(ledger.workspace or cwd, cwd))]
        self._ledger_block = _text(self._render_ledger())
        self._blocks: list[dict[str, Any]] = []
        self.compactions = 0

    # ── reading ─────────────────────────────────────────────────────────────
    def snapshot(self) -> Snapshot:
        """The prefix every fork of this pass shares, byte for byte."""
        return Snapshot(system=tuple(self._system),
                        blocks=(self._ledger_block, *self._blocks))

    @property
    def chars(self) -> int:
        return (len(self._system[0]["text"]) + len(self._ledger_block["text"])
                + sum(len(b["text"]) for b in self._blocks))

    @property
    def fill(self) -> float:
        return self.chars / max(1, int(self.cfg.get("window.budget_chars", 400000)))

    @property
    def approx_tokens(self) -> int:
        return int(self.chars / CHARS_PER_TOKEN)

    # ── appending (the only mutation) ───────────────────────────────────────
    def append_episode(self, observations: list[Observation], pass_number: int) -> int:
        """Append one episode's observations as a single new block."""
        rendered = [o.render() for o in observations if o.kind != META]
        if not rendered:
            return 0
        stamp = time.strftime("%H:%M:%S", time.localtime())
        body = f"--- observations, pass {pass_number} at {stamp} ---\n" + "\n".join(rendered)
        self._blocks.append(_text(body))
        return len(body)

    def append_note(self, note: str) -> None:
        """Append a worker-generated line (a task boundary, a structural trigger)."""
        if note.strip():
            self._blocks.append(_text(note.strip()))

    def append_feedback(self, lines: list[str], pass_number: int) -> None:
        """Append this pass's per-detector conclusions — never their tool calls.

        This is what makes the next pass both cheap and better: a fork opens with
        its own history visible, so it reasons incrementally instead of re-deriving
        the session. Only conclusions accumulate.
        """
        if not lines:
            return
        stamp = time.strftime("%H:%M", time.localtime())
        body = f"--- detector feedback, pass {pass_number} at {stamp} ---\n" + "\n".join(lines)
        self._blocks.append(_text(body))

    # ── compaction: the one sanctioned prefix rebuild ───────────────────────
    def needs_compaction(self) -> bool:
        return self.fill >= float(self.cfg.get("window.compaction_threshold", 0.85))

    async def compact(self, provider: Any, log: Any = None) -> bool:
        """Distil the oldest blocks into the ledger and rebuild the prefix.

        Hysteresis: evict down to the *floor*, not to the trigger. Returns True if
        the window changed, which invalidates the cached prefix exactly once.
        """
        budget = int(self.cfg.get("window.budget_chars", 400000))
        floor_chars = int(budget * float(self.cfg.get("window.compaction_floor", 0.60)))
        overhead = len(self._system[0]["text"]) + len(self._ledger_block["text"])

        keep_from = 0
        running = overhead + sum(len(b["text"]) for b in self._blocks)
        while keep_from < len(self._blocks) and running > floor_chars:
            running -= len(self._blocks[keep_from]["text"])
            keep_from += 1
        evicted = self._blocks[:keep_from]
        if not evicted:
            return False

        summary = await self._summarize(provider, evicted, log)
        if summary:
            self.ledger.add_entry(summary, kind="compaction")
        else:
            # Never silently truncate: what left the window must be visible as
            # having left, even when the summarizer could not be reached.
            self.ledger.add_entry(
                f"[{len(evicted)} observation blocks dropped at compaction without a summary — "
                f"the summarizer was unavailable]", kind="compaction-gap")
        self.ledger.save(int(self.cfg.get("ledger.max_entries", 60)))

        self._blocks = self._blocks[keep_from:]
        self._ledger_block = _text(self._render_ledger())
        self.compactions += 1
        if log:
            log.info("compacted window: %d blocks distilled, fill %.2f", len(evicted), self.fill)
        return True

    async def _summarize(self, provider: Any, blocks: list[dict[str, Any]], log: Any) -> str:
        text = "\n\n".join(b["text"] for b in blocks)
        try:
            reply = await provider.send(
                system=[_text(COMPACTION_SYSTEM)],
                messages=[{"role": "user", "content": [_text(text[-120000:])]}],
                max_tokens=1024,
            )
            return reply.text.strip()
        except Exception as exc:                                   # noqa: BLE001
            if log:
                log.warning("compaction summary failed: %s", exc)
            return ""

    def refresh_ledger_block(self) -> None:
        """Re-render the ledger block. Only ever called as part of a compaction."""
        self._ledger_block = _text(self._render_ledger())

    def _render_ledger(self) -> str:
        digest = self.ledger.digest()
        return f"=== task ledger ===\n{digest}" if digest else "=== task ledger ===\n(new task)"

"""A fork: one detector, one question, one private tail on a shared prefix.

Not a process, not a thread, not an SDK session, and not a subagent. **A fork is a
request to the provider that shares the pass's message prefix** — "fork" describes
the shape of the data, not a runtime object (DESIGN.md §What a "fork" actually is).
Every property the fan-out leans on falls out of that rather than being enforced
somewhere:

- the prefix is shared **by reference** and never mutated, so all N requests are
  byte-identical up to the cache mark;
- a fork's tool results live only in its own local message list, discarded when it
  returns — the isolation is a Python list going out of scope;
- copy-on-write is logical, never materialised: a fork's marginal memory is its own
  few messages;
- cancellation is task cancellation, which is what the hard deadline does.

There is **one loop implementation, instantiated N times**, parameterised by the
detector: its instructions, its grant, iteration cap, deadline, length budget.
Adding a detector adds a config entry, never a code path.

Everything detector-specific lives in the fork's private tail, because the
provider's cache key is a byte-prefix over **tools → system → messages**: a
per-fork system block or a per-fork tools array sits *before* the message-level
breakpoint and would give every fork a different prefix, silently destroying the
one-write-N-reads economics of the fan-out. So the wire-level `tools` list is the
pass's union (built once by the loop) and the system block is the shared one only;
which tools a detector may actually *use* is its grant — stated in its tail and
enforced at dispatch, where every call is re-checked.

The fork is told both its budgets in its own prompt rather than only having them
enforced from outside. A model told it has eight seconds and 400 characters writes
a headline and its evidence; a model told nothing writes four paragraphs that then
get cut, losing whichever part happened to be last.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .constants import FEEDBACK_TOOL
from .detectors import Detector
from .provider import ProviderError, Reply, ToolCall, Usage
from .window import Snapshot


@dataclass
class Outcome:
    """A verdict on one of this detector's own outstanding advisories."""

    advice_id: str
    verdict: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class Feedback:
    """What a fork returns. Only this — never its tool calls — enters the window."""

    detector: str
    verdict: str = "silent"
    headline: str = ""
    body: str = ""
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0
    dedup_key: str = ""
    stale_if: list[str] = field(default_factory=list)
    finish_gate: bool = False
    outcomes: list[Outcome] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    tool_calls: int = 0
    checked: list[str] = field(default_factory=list)
    error: str = ""
    timed_out: bool = False
    elapsed_s: float = 0.0

    def line(self, pass_number: int) -> str:
        """The one compact line merged back into Window B."""
        stamp = time.strftime("%H:%M", time.localtime())
        head = f"[pass {pass_number}, {stamp}] {self.detector} → "
        if self.timed_out:
            return head + "timed out (no answer this pass)"
        if self.error:
            return head + f"failed ({self.error[:80]})"
        if self.verdict == "advise":
            return (head + f'advise "{self.headline[:120]}" '
                    f"({self.confidence:.2f}, key {self.dedup_key or 'none'})")
        checked = f" (checked: {'; '.join(self.checked[:2])})" if self.checked else ""
        return head + self.verdict + checked


def _tail_block(detector: Detector, allowed: list[str], open_advice: list[dict[str, Any]],
                deadline_s: float, body_cap: int, has_new: bool) -> dict[str, Any]:
    """The fork's private tail: its instructions, its grant, its budget, its open advisories.

    This is the ONLY place a fork differs from its siblings — everything before it
    (tools, system, window) is byte-identical across the pass, which is what the
    prefix cache keys on.
    """
    parts = [detector.suffix()]
    if allowed:
        parts.append(
            "Of the tools offered, you may use only: " + ", ".join(allowed)
            + f", plus {FEEDBACK_TOOL}. A call to any other tool will be refused."
        )
    else:
        parts.append(
            f"You have no tools this pass beyond {FEEDBACK_TOOL}; judge from the window alone."
        )
    parts.append(
        f"You have about {int(deadline_s)} seconds and about {body_cap} characters for your "
        f"answer. Spend the time on checking, not on writing; a headline and its evidence is "
        f"the whole shape of a good answer.",
    )
    if open_advice:
        rendered = "\n".join(
            f"- [{a['id']}] {a['headline']} (sent {int(time.time() - a['at'])}s ago)"
            for a in open_advice
        )
        parts.append(
            "Advisories you sent that are still open. Judge each from what the primary did "
            "afterwards and report them in `outcomes`. Evidence is required for any verdict "
            "other than no_evidence, and no_evidence is the default:\n" + rendered
        )
    parts.append(
        "This pass's input is the newest observations block above." if has_new
        else "No new observations this pass; you are being asked on schedule."
    )
    parts.append(
        f"Answer now by calling {FEEDBACK_TOOL} exactly once. `silent` is the expected verdict."
    )
    return {"type": "text", "text": "\n\n".join(parts)}


def _parse(detector: str, call: ToolCall) -> Feedback:
    args = call.input or {}
    outcomes = []
    for raw in args.get("outcomes") or []:
        if isinstance(raw, dict) and raw.get("advice_id"):
            outcomes.append(Outcome(
                advice_id=str(raw["advice_id"]), verdict=str(raw.get("verdict", "no_evidence")),
                evidence=[str(e) for e in (raw.get("evidence") or [])][:4],
            ))
    return Feedback(
        detector=detector,
        verdict=str(args.get("verdict", "silent")),
        headline=str(args.get("headline", "") or ""),
        body=str(args.get("body", "") or ""),
        evidence=[str(e) for e in (args.get("evidence") or [])][:6],
        confidence=float(args.get("confidence", 0.0) or 0.0),
        dedup_key=str(args.get("dedup_key", "") or ""),
        stale_if=[str(s) for s in (args.get("stale_if") or [])][:6],
        finish_gate=bool(args.get("finish_gate", False)),
        outcomes=outcomes,
    )


async def run(detector: Detector, snapshot: Snapshot, *, provider: Any, toolbox: Any,
              tools: list[dict[str, Any]], has_new: bool,
              open_advice: list[dict[str, Any]], deadline_s: float,
              body_cap: int, max_iterations: int, max_output_tokens: int,
              log: Any = None) -> Feedback:
    """Run one detector fork to its feedback call, its iteration cap, or its error.

    `tools` is the pass-level union, identical for every fork — tools sit at the
    front of the provider's cache key, so any per-fork difference there would give
    each fork its own prefix and no fork would ever read another's cache write.
    What this detector may actually call is its grant, re-checked at dispatch.
    """
    started = time.monotonic()
    system = list(snapshot.system)                  # shared, byte-identical per pass
    allowed = [t["name"] for t in toolbox.definitions(detector.grant)]
    tail = _tail_block(detector, allowed, open_advice, deadline_s, body_cap, has_new)
    messages = snapshot.messages_for(tail)          # prefix shared by reference
    usage = Usage()
    tool_calls = 0
    checked: list[str] = []

    budget = max(1, max_iterations)
    for iteration in range(budget):
        # On the last iteration the feedback call is forced via tool_choice. A
        # tool-using detector otherwise spends its whole budget looking things up
        # and never answers — the fork is paid for in full and returns nothing,
        # which is the worst of both outcomes. tool_choice, not a shrunken tools
        # array: changing the tools array invalidates the whole prefix cache,
        # while tool_choice costs only the messages tier of this one request.
        force = FEEDBACK_TOOL if iteration == budget - 1 else ""
        try:
            reply: Reply = await provider.send(
                system=system, messages=messages, tools=tools,
                max_tokens=max_output_tokens, cache_marks=snapshot.cache_marks,
                force_tool=force,
            )
        except ProviderError as exc:
            return Feedback(detector=detector.name, error=str(exc), usage=usage,
                            elapsed_s=time.monotonic() - started)
        usage = usage + reply.usage

        feedback_call = next((c for c in reply.tool_calls if c.name == FEEDBACK_TOOL), None)
        if feedback_call is not None:
            feedback = _parse(detector.name, feedback_call)
            feedback.usage = usage
            feedback.tool_calls = tool_calls
            feedback.checked = checked
            feedback.elapsed_s = time.monotonic() - started
            return feedback

        if not reply.tool_calls:
            # A fork that answered in prose gave no verdict. Treat as silence: the
            # schema is the contract, and inventing a finding out of free text is
            # exactly the unchecked path the schema exists to close.
            return Feedback(detector=detector.name, verdict="silent", usage=usage,
                            tool_calls=tool_calls, checked=checked,
                            elapsed_s=time.monotonic() - started)

        results = []
        for call in reply.tool_calls:
            tool_calls += 1
            output = await toolbox.dispatch(call.name, call.input, detector.grant)
            checked.append(f"{call.name} {str(list(call.input.values())[:1])[:60]}")
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": output})
        # Tool results stay in THIS fork's list and are discarded when it returns.
        messages = [
            *messages,
            {"role": "assistant", "content": [
                *([{"type": "text", "text": reply.text}] if reply.text.strip() else []),
                *[{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
                  for c in reply.tool_calls],
            ]},
            {"role": "user", "content": results},
        ]

    if log:
        log.info("%s hit its iteration cap without answering", detector.name)
    return Feedback(detector=detector.name, verdict="silent", usage=usage,
                    tool_calls=tool_calls, checked=checked,
                    error="iteration cap", elapsed_s=time.monotonic() - started)


async def run_with_deadline(detector: Detector, snapshot: Snapshot, *, hard_deadline_s: float,
                            **kwargs: Any) -> Feedback:
    """Two-tier timeout: the soft deadline is in the prompt, this is the hard cancel.

    The gap between them is what makes a graceful answer the normal path and
    cancellation the exception — a model that knows it is nearly out of time
    answers; a model that is simply killed contributes nothing.
    """
    started = time.monotonic()
    try:
        return await asyncio.wait_for(run(detector, snapshot, **kwargs), timeout=hard_deadline_s)
    except asyncio.TimeoutError:
        return Feedback(detector=detector.name, verdict="silent", timed_out=True,
                        elapsed_s=time.monotonic() - started)

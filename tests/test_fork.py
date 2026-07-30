"""Detector fixtures — the fan-out, against a scripted provider.

What is worth asserting here is not that a model says something sensible (that is
§Did it land?, and it needs a live session) but that the *execution model* holds:
every fork of a pass sends a byte-identical prefix — tools, system, and shared
message blocks alike, since the provider's cache key is a byte-prefix over
tools → system → messages — a fork's tool results stay in that fork, only
conclusions reach the window, the pilot goes first and alone, and a straggler is
demoted rather than tolerated.
"""
from __future__ import annotations

import asyncio

import pytest

from second_brain.constants import FEEDBACK_TOOL
from second_brain.detectors import FEEDBACK_SCHEMA, Detector, pick_pilot, resolve
from second_brain.fork import run, run_with_deadline
from second_brain.ledger import Ledger
from second_brain.provider import Reply, ToolCall, Usage
from second_brain.tools import Toolbox, parse_grants, union_grants
from second_brain.window import Window


class FakeProvider:
    """Records every request and replies from a script."""

    def __init__(self, script=None, honor_tools=True):
        self.requests: list[dict] = []
        self.script = script or {}
        self.delay = 0.0
        # Real models can only call what they were offered. One test deliberately
        # simulates a model naming a tool it never got, to prove dispatch re-checks.
        self.honor_tools = honor_tools

    async def send(self, system, messages, tools=None, max_tokens=1024, cache_marks=None,
                   force_tool=""):
        self.requests.append({"system": system, "messages": messages, "tools": tools,
                              "cache_marks": cache_marks, "force_tool": force_tool})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.honor_tools and force_tool == FEEDBACK_TOOL:
            # A forced tool_choice leaves the model no alternative: once the
            # verdict is demanded, the fake answers rather than reaching further.
            return _silent()
        tail = _tail_text(messages)
        detector = tail.split("'")[1] if "'" in tail else "?"
        replies = self.script.get(detector) or self.script.get("*") or [_silent()]
        index = min(len([r for r in self.requests
                         if detector in _tail_text(r["messages"])]) - 1, len(replies) - 1)
        return replies[index]


def _tail_text(messages) -> str:
    """The fork's private tail — the last block of the first (window) message."""
    return messages[0]["content"][-1]["text"]


def _silent():
    return Reply(tool_calls=[ToolCall(id="t1", name=FEEDBACK_TOOL, input={"verdict": "silent"})],
                 usage=Usage(input_tokens=10, output_tokens=5))


def _advise(headline="tests never ran", key="tests-not-run", confidence=0.8):
    return Reply(tool_calls=[ToolCall(id="t1", name=FEEDBACK_TOOL, input={
        "verdict": "advise", "headline": headline, "confidence": confidence,
        "dedup_key": key, "evidence": ["Edit a.go @L1"], "stale_if": ["go test"],
    })], usage=Usage(input_tokens=10, output_tokens=20))


def _tool_then_silent(name="Read", args=None):
    return [Reply(tool_calls=[ToolCall(id="c1", name=name, input=args or {"file_path": "a.txt"})],
                  usage=Usage(input_tokens=5, output_tokens=5)), _silent()]


@pytest.fixture
def window(cfg):
    ledger = Ledger(task_id="t-1", workspace="/repo", goal="add health probes")
    window = Window(cfg, ledger, "/repo")
    window.append_episode(_observations(), 1)
    return window


def _observations():
    from second_brain.projection import TEXT, TOOL, Observation
    return [Observation(kind=TEXT, body="adding the health trio"),
            Observation(kind=TOOL, tool="Edit", body="Edit services/foo/health.go @L12")]


def detector(name="default", tools=None, **kw):
    return Detector(name=name, system="watch", grant=parse_grants(tools or []), **kw)


def pass_tools(toolbox, detectors):
    """What the loop builds once per pass: the union grant plus the schema."""
    return [*toolbox.definitions(union_grants(d.grant for d in detectors)), FEEDBACK_SCHEMA]


def _run(det, snapshot, provider, toolbox, *, tools=None, has_new=True, open_advice=None,
         deadline_s=5, body_cap=400, max_iterations=3, max_output_tokens=256):
    return run(det, snapshot, provider=provider, toolbox=toolbox,
               tools=tools if tools is not None else pass_tools(toolbox, [det]),
               has_new=has_new, open_advice=open_advice or [], deadline_s=deadline_s,
               body_cap=body_cap, max_iterations=max_iterations,
               max_output_tokens=max_output_tokens)


# ── the invariant the whole fan-out rests on ────────────────────────────────
def test_every_fork_sends_a_byte_identical_prefix(window, tmp_path):
    provider = FakeProvider()
    toolbox = Toolbox(tmp_path)
    snapshot = window.snapshot()
    detectors = [detector("repeat-failure"), detector("standard-questions", tools=["Read"]),
                 detector("default", tools=["Grep"])]
    shared_tools = pass_tools(toolbox, detectors)

    async def go():
        for det in detectors:
            await _run(det, snapshot, provider, toolbox, tools=shared_tools)
    asyncio.run(go())

    first = provider.requests[0]
    # The whole cache key is shared: tools, system, and the message blocks up to
    # the fork's private tail. Only the tail differs.
    assert all(r["tools"] == first["tools"] for r in provider.requests)
    assert all(r["system"] == first["system"] for r in provider.requests)
    prefixes = [r["messages"][0]["content"][:-1] for r in provider.requests]
    assert all(p == prefixes[0] for p in prefixes)
    assert len({_tail_text(r["messages"]) for r in provider.requests}) == 3


def test_the_cache_mark_sits_at_the_end_of_the_shared_prefix(window, tmp_path):
    snapshot = window.snapshot()
    provider = FakeProvider()
    asyncio.run(_run(detector(), snapshot, provider, Toolbox(tmp_path), max_iterations=2))
    [(message_index, block_index)] = provider.requests[0]["cache_marks"]
    content = provider.requests[0]["messages"][message_index]["content"]
    assert block_index == len(content) - 2          # everything after it is this fork's tail


def test_the_episode_is_inside_the_shared_prefix_not_the_tail(window, tmp_path):
    """The current pass's input must be cacheable — it lives in the window blocks,
    before the breakpoint, not in the per-fork tail where no fork could share it."""
    provider = FakeProvider()
    asyncio.run(_run(detector(), window.snapshot(), provider, Toolbox(tmp_path)))
    content = provider.requests[0]["messages"][0]["content"]
    shared = "".join(b["text"] for b in content[:-1])
    assert "health.go" in shared                    # the observation is shared…
    assert "health.go" not in content[-1]["text"]   # …and not repeated in the tail


def test_a_forks_tool_results_stay_inside_that_fork(window, tmp_path):
    (tmp_path / "a.txt").write_text("hello from disk")
    provider = FakeProvider({"*": _tool_then_silent()})
    feedback = asyncio.run(_run(detector(tools=["Read"]), window.snapshot(), provider,
                                Toolbox(tmp_path)))
    assert feedback.tool_calls == 1
    # The tool result exists in the second request's messages …
    assert "hello from disk" in str(provider.requests[1]["messages"])
    # … and nowhere in what goes back to the window.
    assert "hello from disk" not in feedback.line(1)
    assert "hello from disk" not in "".join(b["text"] for b in window.snapshot().blocks)


# ── the grant is the boundary, and it is re-checked ─────────────────────────
def test_a_detector_without_a_grant_cannot_read(window, tmp_path):
    """The pass union may offer Read (another detector's grant) — dispatch still
    refuses a fork whose own grant does not carry it."""
    (tmp_path / "secret.txt").write_text("nope")
    provider = FakeProvider({"*": _tool_then_silent()}, honor_tools=False)
    toolbox = Toolbox(tmp_path)
    unarmed = detector("unarmed", tools=[])
    shared = pass_tools(toolbox, [unarmed, detector("armed", tools=["Read"])])
    asyncio.run(_run(unarmed, window.snapshot(), provider, toolbox, tools=shared))
    assert "not available to this detector" in str(provider.requests[1]["messages"])
    # …and its tail says so, so the model is not invited to try.
    assert "no tools this pass" in _tail_text(provider.requests[0]["messages"])


def test_the_tail_names_the_tools_this_fork_may_use(window, tmp_path):
    provider = FakeProvider()
    toolbox = Toolbox(tmp_path)
    armed = detector("armed", tools=["Read"])
    shared = pass_tools(toolbox, [armed, detector("other", tools=["Grep"])])
    asyncio.run(_run(armed, window.snapshot(), provider, toolbox, tools=shared))
    offered = {t["name"] for t in provider.requests[0]["tools"]}
    assert offered == {"Read", "Grep", FEEDBACK_TOOL}   # the union goes on the wire
    tail = _tail_text(provider.requests[0]["messages"])
    assert "you may use only: Read" in tail             # the grant goes in the tail
    assert "Grep" not in tail


def test_the_path_jail_refuses_a_read_outside_the_workspace(tmp_path):
    box = Toolbox(tmp_path)
    grant = parse_grants(["Read"])
    result = asyncio.run(box.dispatch("Read", {"file_path": "/etc/passwd"}, grant))
    assert "outside the observed workspace" in result


def test_an_unlisted_command_is_refused_at_dispatch(tmp_path):
    box = Toolbox(tmp_path)
    grant = parse_grants([{"exec": ["git status --short"]}])
    allowed = asyncio.run(box.dispatch("Run", {"command": "git status --short"}, grant))
    refused = asyncio.run(box.dispatch("Run", {"command": "rm -rf /"}, grant))
    assert "exit 0" in allowed or "exit 128" in allowed
    assert "not allowed for this detector" in refused


def test_tool_definitions_carry_only_what_was_granted(tmp_path):
    box = Toolbox(tmp_path)
    names = {t["name"] for t in box.definitions(parse_grants(["Read", {"exec": ["go build ./..."]}]))}
    assert names == {"Read", "Run"}
    assert box.definitions(parse_grants([])) == []


def test_union_grants_is_deterministic_and_complete(tmp_path):
    a = parse_grants(["Read", {"exec": ["go vet ./..."]}])
    b = parse_grants(["Grep", {"exec": ["go build ./...", "go vet ./..."]}])
    union = union_grants([a, b])
    assert union.builtins == {"Read", "Grep"}
    assert union.commands == ["go build ./...", "go vet ./..."]        # sorted, deduped
    assert union_grants([b, a]).commands == union.commands             # order-independent


# ── deadlines and the demotion ladder ───────────────────────────────────────
def test_the_hard_deadline_cancels_a_straggler(window, tmp_path):
    provider = FakeProvider()
    provider.delay = 0.5
    toolbox = Toolbox(tmp_path)
    slow = detector("slow")
    feedback = asyncio.run(run_with_deadline(
        slow, window.snapshot(), hard_deadline_s=0.05, provider=provider,
        toolbox=toolbox, tools=pass_tools(toolbox, [slow]), has_new=True,
        open_advice=[], deadline_s=0.01,
        body_cap=400, max_iterations=2, max_output_tokens=256))
    assert feedback.timed_out is True
    assert feedback.verdict == "silent"
    assert "timed out" in feedback.line(3)


def test_prose_without_a_verdict_is_treated_as_silence(window, tmp_path):
    provider = FakeProvider({"*": [Reply(text="I think maybe they should refactor.")]},
                            honor_tools=False)
    feedback = asyncio.run(_run(detector(), window.snapshot(), provider, Toolbox(tmp_path),
                                max_iterations=2))
    assert feedback.verdict == "silent"


def test_the_fork_is_told_both_of_its_budgets(window, tmp_path):
    provider = FakeProvider()
    asyncio.run(_run(detector(), window.snapshot(), provider, Toolbox(tmp_path),
                     open_advice=[{"id": "a1", "headline": "prior", "at": 0}],
                     deadline_s=9, body_cap=333, max_iterations=2))
    tail = _tail_text(provider.requests[0]["messages"])
    assert "9 seconds" in tail and "333 characters" in tail
    assert "a1" in tail and "no_evidence is the default" in tail


# ── the pilot ───────────────────────────────────────────────────────────────
def test_the_pilot_is_the_cheapest_declared_detector(cfg):
    detectors = [d for d in resolve(cfg.group("detectors")) if d.enabled]
    pilot = pick_pilot(detectors)
    assert pilot is not None
    assert pilot.name == "repeat-failure"
    assert pilot.grant.empty                        # never one that might sit in a build


def test_feedback_lines_are_compact_and_stable():
    from second_brain.fork import Feedback
    line = Feedback(detector="git-log", verdict="silent", checked=["3 commits"]).line(14)
    assert line.startswith("[pass 14")
    assert "git-log → silent (checked: 3 commits)" in line


def test_the_last_iteration_forces_the_verdict_without_touching_the_tools(window, tmp_path):
    """A tool-using detector that never calls the feedback tool is a wasted fork.

    Observed live: `default` spent all six model calls on Read/Grep and returned
    nothing, so the pass paid for a fork and got silence with an error attached.
    On the final iteration the verdict is forced via `tool_choice` — never by
    shrinking the tools array, which sits at the front of the provider's cache
    key and would invalidate the whole shared prefix for that request.
    """
    (tmp_path / "a.txt").write_text("contents")
    greedy = [Reply(tool_calls=[ToolCall(id=f"c{i}", name="Read", input={"file_path": "a.txt"})],
                    usage=Usage(input_tokens=5, output_tokens=5)) for i in range(5)]
    provider = FakeProvider({"*": greedy})

    feedback = asyncio.run(_run(detector(tools=["Read"]), window.snapshot(), provider,
                                Toolbox(tmp_path)))
    first, last = provider.requests[0], provider.requests[-1]
    assert last["tools"] == first["tools"]           # the tools array never changes
    assert first["force_tool"] == ""                 # early: it may look things up
    assert last["force_tool"] == FEEDBACK_TOOL       # last: the verdict is demanded
    assert feedback.verdict == "silent" and feedback.error == ""


def test_the_pilot_falls_back_to_a_tool_less_detector_when_disabled(cfg):
    """Disabling the declared pilot must not break the warm-up: the chain is
    declared pilot → first tool-less enabled detector → anyone. Tool-less is
    preferred because everything else waits on the pilot, and a fork that might
    sit in tools stretches the critical path."""
    from second_brain.detectors import enabled as enabled_detectors

    cfg.values["detectors"]["repeat-failure"]["enabled"] = False
    detectors = enabled_detectors(resolve(cfg.group("detectors")))
    assert all(not d.pilot for d in detectors)
    pilot = pick_pilot(detectors)
    assert pilot is not None
    assert pilot.name == "standard-questions"       # tool-less beats tool-using
    assert pilot.grant.empty


def test_a_tool_using_pilot_is_the_last_resort(cfg):
    """With only tool-armed detectors enabled, one of them still pilots: the
    prefix must be written once by SOMEONE before the fan-out, tools or not."""
    from second_brain.detectors import enabled as enabled_detectors

    for name, spec in cfg.values["detectors"].items():
        spec["enabled"] = name == "default"
    detectors = enabled_detectors(resolve(cfg.group("detectors")))
    assert [d.name for d in detectors] == ["default"]
    pilot = pick_pilot(detectors)
    assert pilot is not None and pilot.name == "default"
    assert not pilot.grant.empty

"""Detector fixtures — the fan-out, against a scripted provider.

What is worth asserting here is not that a model says something sensible (that is
§Did it land?, and it needs a live session) but that the *execution model* holds:
every fork of a pass sends a byte-identical prefix, a fork's tool results stay in
that fork, only conclusions reach the window, the pilot goes first and alone, and a
straggler is demoted rather than tolerated.
"""
from __future__ import annotations

import asyncio

import pytest

from second_brain.constants import FEEDBACK_TOOL
from second_brain.detectors import Detector, pick_pilot, resolve
from second_brain.fork import run, run_with_deadline
from second_brain.ledger import Ledger
from second_brain.provider import Reply, ToolCall, Usage
from second_brain.tools import Toolbox, parse_grants
from second_brain.window import Window


class FakeProvider:
    """Records every request and replies from a script."""

    def __init__(self, script=None):
        self.requests: list[dict] = []
        self.script = script or {}
        self.delay = 0.0

    async def send(self, system, messages, tools=None, max_tokens=1024, cache_marks=None):
        self.requests.append({"system": system, "messages": messages, "tools": tools,
                              "cache_marks": cache_marks})
        if self.delay:
            await asyncio.sleep(self.delay)
        detector = system[-1]["text"].split("'")[1] if "'" in system[-1]["text"] else "?"
        replies = self.script.get(detector) or self.script.get("*") or [_silent()]
        index = min(len([r for r in self.requests
                         if detector in r["system"][-1]["text"]]) - 1, len(replies) - 1)
        return replies[index]


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


# ── the invariant the whole fan-out rests on ────────────────────────────────
def test_every_fork_sends_a_byte_identical_prefix(window, tmp_path):
    provider = FakeProvider()
    toolbox = Toolbox(tmp_path)
    snapshot = window.snapshot()

    async def go():
        for name in ("repeat-failure", "standard-questions", "default"):
            await run(detector(name), snapshot, provider=provider, toolbox=toolbox,
                      episode="[text] something", open_advice=[], deadline_s=5,
                      body_cap=400, max_iterations=3, max_output_tokens=256)
    asyncio.run(go())

    prefixes = [r["messages"][0]["content"][:-1] for r in provider.requests]
    assert all(p == prefixes[0] for p in prefixes)
    # …and the shared system block is identical too; only the suffix differs.
    assert all(r["system"][0] == provider.requests[0]["system"][0] for r in provider.requests)
    assert len({r["system"][-1]["text"] for r in provider.requests}) == 3


def test_the_cache_mark_sits_at_the_end_of_the_shared_prefix(window, tmp_path):
    snapshot = window.snapshot()
    provider = FakeProvider()
    asyncio.run(run(detector(), snapshot, provider=provider, toolbox=Toolbox(tmp_path),
                    episode="x", open_advice=[], deadline_s=5, body_cap=400,
                    max_iterations=2, max_output_tokens=256))
    [(message_index, block_index)] = provider.requests[0]["cache_marks"]
    content = provider.requests[0]["messages"][message_index]["content"]
    assert block_index == len(content) - 2          # everything after it is this fork's tail


def test_a_forks_tool_results_stay_inside_that_fork(window, tmp_path):
    (tmp_path / "a.txt").write_text("hello from disk")
    provider = FakeProvider({"*": _tool_then_silent()})
    feedback = asyncio.run(run(detector(tools=["Read"]), window.snapshot(), provider=provider,
                               toolbox=Toolbox(tmp_path), episode="x", open_advice=[],
                               deadline_s=5, body_cap=400, max_iterations=3,
                               max_output_tokens=256))
    assert feedback.tool_calls == 1
    # The tool result exists in the second request's messages …
    assert "hello from disk" in str(provider.requests[1]["messages"])
    # … and nowhere in what goes back to the window.
    assert "hello from disk" not in feedback.line(1)
    assert "hello from disk" not in "".join(b["text"] for b in window.snapshot().blocks)


# ── the grant is the boundary, and it is re-checked ─────────────────────────
def test_a_detector_without_a_grant_cannot_read(window, tmp_path):
    (tmp_path / "secret.txt").write_text("nope")
    provider = FakeProvider({"*": _tool_then_silent()})
    asyncio.run(run(detector(tools=[]), window.snapshot(), provider=provider,
                    toolbox=Toolbox(tmp_path), episode="x", open_advice=[], deadline_s=5,
                    body_cap=400, max_iterations=3, max_output_tokens=256))
    assert "not available to this detector" in str(provider.requests[1]["messages"])


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


# ── deadlines and the demotion ladder ───────────────────────────────────────
def test_the_hard_deadline_cancels_a_straggler(window, tmp_path):
    provider = FakeProvider()
    provider.delay = 0.5
    feedback = asyncio.run(run_with_deadline(
        detector("slow"), window.snapshot(), hard_deadline_s=0.05, provider=provider,
        toolbox=Toolbox(tmp_path), episode="x", open_advice=[], deadline_s=0.01,
        body_cap=400, max_iterations=2, max_output_tokens=256))
    assert feedback.timed_out is True
    assert feedback.verdict == "silent"
    assert "timed out" in feedback.line(3)


def test_prose_without_a_verdict_is_treated_as_silence(window, tmp_path):
    provider = FakeProvider({"*": [Reply(text="I think maybe they should refactor.")]})
    feedback = asyncio.run(run(detector(), window.snapshot(), provider=provider,
                               toolbox=Toolbox(tmp_path), episode="x", open_advice=[],
                               deadline_s=5, body_cap=400, max_iterations=2,
                               max_output_tokens=256))
    assert feedback.verdict == "silent"


def test_the_fork_is_told_both_of_its_budgets(window, tmp_path):
    provider = FakeProvider()
    asyncio.run(run(detector(), window.snapshot(), provider=provider, toolbox=Toolbox(tmp_path),
                    episode="x", open_advice=[{"id": "a1", "headline": "prior", "at": 0}],
                    deadline_s=9, body_cap=333, max_iterations=2, max_output_tokens=256))
    tail = provider.requests[0]["messages"][0]["content"][-1]["text"]
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

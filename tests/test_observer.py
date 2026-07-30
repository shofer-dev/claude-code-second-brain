"""The loop, end to end, with a scripted provider: spool → pass → gate → mailbox.

This is the Phase 1 and Phase 2 proof in one file — that the whole path works
without a real model, that a pass is single-flight and governed by two limits, and
that the loop's failure modes are all "degrade to silence" rather than anything the
watched session could notice.
"""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from second_brain import mailbox, spool
from second_brain.constants import FEEDBACK_TOOL
from second_brain.loop import Observer
from second_brain.projection import ERROR, META, TEXT, TOOL, Observation
from second_brain.provider import ProviderError, Reply, ToolCall, Usage
from tests.test_fork import FakeProvider, _advise, _silent


@pytest.fixture
def observer(tmp_path):
    emitted: list[str] = []
    log = logging.getLogger("test-second-brain")
    log.addHandler(logging.NullHandler())
    obs = Observer("s-obs", str(tmp_path), "/repo", emit=emitted.append, log=log,
                   hosted_by="monitor")
    obs.emitted = emitted                            # type: ignore[attr-defined]
    # Detectors are the pilot plus one, so the fan-out is exercised without noise.
    for name in list(obs.cfg.values["detectors"]):
        obs.cfg.values["detectors"][name]["enabled"] = name in {"repeat-failure", "default"}
    obs.cfg.values["loop"]["min_interval_s"] = 0
    obs.cfg.values["loop"]["trigger_chars"] = 10
    return obs


def feed(session: str, *observations: Observation) -> None:
    spool.append(session, list(observations))


def text(body: str = "adding the health trio") -> Observation:
    return Observation(kind=TEXT, ts=time.time(), body=body, raw_chars=len(body))


def tool(name: str = "Edit", body: str = "Edit a.go @L1", **meta) -> Observation:
    return Observation(kind=TOOL, ts=time.time(), tool=name, body=body,
                       locators=["a.go"], raw_chars=len(body), meta=meta)


def meta(event: str, **fields) -> Observation:
    return Observation(kind=META, ts=time.time(), body=event, meta={"event": event, **fields})


def tick(observer: Observer) -> None:
    asyncio.run(observer._tick())


# ── the happy path ──────────────────────────────────────────────────────────
def test_a_pass_turns_observations_into_a_gated_advisory(observer):
    observer.hosted_by = "hook"          # no monitor push, so the mailbox is inspectable
    observer.provider = FakeProvider({"default": [_advise()], "repeat-failure": [_silent()]})
    feed("s-obs", text(), tool())
    tick(observer)

    assert observer.pass_number == 1
    pending = mailbox.peek("s-obs")
    assert [a.kind for a in pending] == ["default"]
    assert pending[0].headline == "tests never ran"
    # Conclusions reach the window; the episode does too, and in that order.
    blocks = [b["text"] for b in observer.window.snapshot().blocks]
    assert any("adding the health trio" in b for b in blocks)
    assert any("default → advise" in b for b in blocks)
    assert any("repeat-failure → silent" in b for b in blocks)


def test_silence_is_the_common_case_and_costs_nothing_downstream(observer):
    observer.provider = FakeProvider({"*": [_silent()]})
    feed("s-obs", text(), tool())
    tick(observer)
    assert mailbox.peek("s-obs") == []
    assert observer.status.advisories_generated == 0


def test_the_monitor_pushes_and_the_push_is_observed_as_delivery(observer):
    observer.provider = FakeProvider({"default": [_advise()], "repeat-failure": [_silent()]})
    feed("s-obs", text(), tool())
    tick(observer)                                    # pass, gate and push, in one tick

    assert observer.emitted                           # type: ignore[attr-defined]
    pushed = observer.emitted[0]                      # type: ignore[attr-defined]
    assert "tests never ran" in pushed
    assert "Second Brain advisory" in pushed          # the agent's framed copy …
    assert "detectors.default.enabled false" in pushed   # … and the user's, in one line
    assert mailbox.peek("s-obs") == []                # claimed exactly once


def test_a_delivered_advisory_opens_an_outcome_record(observer):
    observer.provider = FakeProvider({"default": [_advise()], "repeat-failure": [_silent()]})
    feed("s-obs", text(), tool())
    tick(observer)                                    # pass → gate → push
    tick(observer)                                    # ingests the delivery record
    assert len(observer.open_advice) == 1
    assert next(iter(observer.open_advice.values())).detector == "default"


def test_an_outcome_needs_evidence_or_it_closes_as_no_evidence(observer):
    from second_brain.loop import OpenAdvice
    observer.open_advice = {"a1": OpenAdvice("a1", "default", "h", time.time())}
    observer.ledger.record_advice("a1", "default", "h", "k")

    from second_brain.fork import Outcome
    observer._close_outcome(Outcome("a1", "adopted", []))          # flattering, unevidenced
    assert observer.ledger.advised[-1]["verdict"] == "no_evidence"

    observer.open_advice = {"a2": OpenAdvice("a2", "default", "h", time.time())}
    observer.ledger.record_advice("a2", "default", "h", "k2")
    observer._close_outcome(Outcome("a2", "adopted", ["Bash: go test ./... @ 14:22"]))
    assert observer.ledger.advised[-1]["verdict"] == "adopted"


# ── the trigger policy ──────────────────────────────────────────────────────
def test_the_clock_floor_binds_however_much_the_primary_emits(observer):
    observer.provider = FakeProvider({"*": [_silent()]})
    observer.cfg.values["loop"]["min_interval_s"] = 3600
    observer.last_pass_start = time.time()
    feed("s-obs", *[text("x" * 500) for _ in range(50)])
    tick(observer)
    assert observer.pass_number == 0                  # a burst does not become a pass storm
    assert observer.episode                           # …it accumulates instead


def test_a_salient_event_may_fire_early_but_only_from_its_bucket(observer):
    observer.provider = FakeProvider({"*": [_silent()]})
    observer.cfg.values["loop"]["trigger_chars"] = 10_000_000
    observer.cfg.values["loop"]["salience_per_hour"] = 1

    feed("s-obs", Observation(kind=ERROR, tool="Bash", body="exit 1"))
    tick(observer)
    assert observer.pass_number == 1

    feed("s-obs", Observation(kind=ERROR, tool="Bash", body="exit 1 again"))
    tick(observer)
    assert observer.pass_number == 1                  # the hourly bucket is spent


def test_a_burst_coalesces_instead_of_growing_without_bound(observer):
    observer.cfg.values["loop"]["min_interval_s"] = 3600
    observer.cfg.values["loop"]["episode_cap_chars"] = 5000
    observer.last_pass_start = time.time()
    feed("s-obs", *[text("y" * 400) for _ in range(60)],
         Observation(kind=ERROR, tool="Bash", body="the error that must survive"))
    tick(observer)
    assert observer.pending_chars <= 20000
    assert any("must survive" in o.body for o in observer.episode)   # errors are kept


# ── degradation ─────────────────────────────────────────────────────────────
def test_an_exhausted_budget_degrades_to_silence(observer):
    observer.provider = FakeProvider({"*": [_advise()]})
    observer.cfg.values["budget"]["tokens_per_task"] = 1
    observer.budget.task_used = 10
    feed("s-obs", text(), tool())
    tick(observer)
    assert observer.pass_number == 0
    assert "budget" in observer.status.state
    assert mailbox.peek("s-obs") == []


def test_a_provider_outage_is_silence_not_a_crash(observer):
    class Broken:
        async def send(self, **kwargs):
            raise ProviderError("503")

    observer.provider = Broken()
    feed("s-obs", text(), tool())
    tick(observer)
    assert mailbox.peek("s-obs") == []
    assert any("adding the health trio" in b["text"] for b in observer.window.snapshot().blocks)


def test_repeated_timeouts_demote_then_disable_a_detector(observer):
    from second_brain.fork import Feedback
    for _ in range(2):
        observer._demotion(Feedback(detector="slow", timed_out=True))
    assert observer.detector_state["slow"].stride == observer.cfg.get("loop.demote_stride")
    for _ in range(2):
        observer._demotion(Feedback(detector="slow", timed_out=True))
    assert observer.detector_state["slow"].disabled_until > time.time()
    assert "disabled" in observer.status.detectors["slow"]["state"]

    observer._demotion(Feedback(detector="slow", timed_out=False))
    assert observer.detector_state["slow"].stride == 1


# ── task boundaries and the finish gate ─────────────────────────────────────
def test_a_task_boundary_resets_the_window_and_the_ledger(observer):
    observer.provider = FakeProvider({"*": [_silent()]})
    feed("s-obs", text(), tool())
    tick(observer)
    first_task = observer.binding.task_id
    observer.ledger.add_entry("something learned in task one")

    feed("s-obs", meta("session_start", source="clear"))
    tick(observer)
    assert observer.binding.task_id != first_task
    assert observer.ledger.entries == []
    assert observer.window.snapshot().blocks[0]["text"].endswith("(new task)")


def test_a_new_goal_mid_session_splits_the_task_and_keeps_the_prompt(observer):
    observer.provider = FakeProvider({"*": [_silent()]})
    observer.binding.started_at = time.time() - 7200
    first_task = observer.binding.task_id
    observer.ledger.add_entry("decided to use the shared health package")

    feed("s-obs", Observation(kind="user", ts=time.time(),
                              body="now let's do something completely different with the deploy pipeline"))
    tick(observer)

    assert observer.binding.task_id != first_task
    assert observer.ledger.entries == []              # task 1's judgment does not carry over
    assert observer.ledger.goal.startswith("now let's do something")
    assert any("deploy pipeline" in b["text"] for b in observer.window.snapshot().blocks)


def test_a_follow_up_prompt_does_not_split_the_task(observer):
    observer.provider = FakeProvider({"*": [_silent()]})
    observer.binding.started_at = time.time() - 7200
    first_task = observer.binding.task_id
    feed("s-obs", Observation(kind="user", ts=time.time(),
                              body="also fix the typo in the comment just above that line"))
    tick(observer)
    assert observer.binding.task_id == first_task


def test_the_finish_gate_stays_silent_while_background_work_is_outstanding(observer):
    feed("s-obs", tool("Bash", "Bash (background)\n make watch", background=True),
         meta("stop"))
    tick(observer)
    assert observer._genuinely_finished() is False

    observer.background.clear()
    feed("s-obs", meta("stop"))
    tick(observer)
    assert observer._genuinely_finished() is True


def test_a_finish_gate_advisory_is_downgraded_when_work_is_outstanding(observer):
    advise_finish = Reply(tool_calls=[ToolCall(id="t", name=FEEDBACK_TOOL, input={
        "verdict": "advise", "headline": "no deploy command observed", "confidence": 0.9,
        "dedup_key": "deploy", "evidence": ["services.json bumped"], "finish_gate": True,
    })], usage=Usage())
    observer.hosted_by = "hook"
    observer.provider = FakeProvider({"default": [advise_finish], "repeat-failure": [_silent()]})
    feed("s-obs", tool("Bash", "Bash (background)\n make watch", background=True), text())
    tick(observer)
    [advisory] = mailbox.peek("s-obs")
    assert advisory.finish_gate is False              # any doubt means it does not interrupt


def test_session_end_stops_the_worker(observer):
    feed("s-obs", meta("session_end", reason="exit"))
    tick(observer)
    assert observer.finished is True


def test_status_is_written_through_for_the_human_surfaces(observer):
    from second_brain import status
    observer.provider = FakeProvider({"*": [_silent()]})
    feed("s-obs", text(), tool())
    tick(observer)
    record = status.read("s-obs")
    assert record is not None
    assert record["passes"] == 1
    assert record["observed_chars"] > 0
    assert "default" in record["detectors"]


def test_a_pass_request_fires_once_and_expires(observer, tmp_path):
    """`/second-brain-run` is a live request, not a queued job.

    The worker holds the code it imported at startup, so a request written while an
    older worker was running is never consumed — and must not fire a surprising pass
    whenever the next worker happens to start.
    """
    import os
    from second_brain import paths
    from second_brain.loop import REQUEST_TTL_S

    observer.provider = FakeProvider({"*": [_silent()]})
    observer.cfg.values["loop"]["min_interval_s"] = 3600      # the throttle is bypassed
    observer.cfg.values["loop"]["trigger_chars"] = 10_000_000  # so is the volume threshold
    observer.last_pass_start = time.time()
    feed("s-obs", text())
    tick(observer)
    assert observer.pass_number == 0                          # nothing is due on its own

    request = paths.trigger_path("s-obs")
    paths.write_private(request, "{}")
    tick(observer)
    assert observer.pass_number == 1
    assert not request.exists()                               # consumed, so it fires once

    paths.write_private(request, "{}")
    stale = time.time() - REQUEST_TTL_S - 60
    os.utime(request, (stale, stale))
    feed("s-obs", text())
    tick(observer)
    assert observer.pass_number == 1                          # too old to honour
    assert not request.exists()                               # …and cleaned up regardless


def test_a_config_change_reaches_the_gate_without_a_restart(observer):
    """`/second-brain-config` promises a running worker picks changes up.

    The Observer, the gate and the window each hold a Config; reloading only the
    Observer's leaves the other two on the previous object, so every `gate.*` and
    `window.*` knob appears set and does nothing.
    """
    from second_brain.config import set_value

    observer.provider = FakeProvider({"*": [_silent()]})
    before = observer.gate.cfg
    set_value("gate.rate_per_hour", 1, scope="global")
    set_value("window.budget_chars", 123_456, scope="global")

    feed("s-obs", text(), tool())
    tick(observer)

    assert observer.gate.cfg is not before          # the gate saw the reload
    assert observer.gate.cfg.get("gate.rate_per_hour") == 1
    assert observer.window.cfg.get("window.budget_chars") == 123_456


def test_a_replayed_control_signal_cannot_change_state(observer):
    """The spool outlives the worker; a restart without an offset replays it.

    Observed live: the first worker to start after the durable-offset fix replayed
    8.7 hours of spool, read a stale `session_start` as genuine, minted a new task
    and orphaned the previous ledger. A replayed `session_end` would have been
    worse — it would have exited the worker.
    """
    stale = time.time() - 86400
    task_before = observer.binding.task_id

    observer._ingest([
        Observation(kind=META, ts=stale, body="session_start",
                    meta={"event": "session_start", "source": "startup"}),
        Observation(kind=META, ts=stale, body="stop", meta={"event": "stop"}),
        Observation(kind=META, ts=stale, body="session_end", meta={"event": "session_end"}),
        Observation(kind="user", ts=stale,
                    body="now let's do something completely different with the deploy pipeline"),
    ])
    assert observer.binding.task_id == task_before   # no task minted or split
    assert observer.turn_stopped is False            # the finish gate is not armed
    assert observer.finished is False                # and the worker is still alive

    # A fresh signal of the same kind is still honoured.
    observer._ingest([Observation(kind=META, ts=time.time(), body="stop", meta={"event": "stop"})])
    assert observer.turn_stopped is True


# ── the debug capture (`debug.enabled`) ─────────────────────────────────────
def test_debug_capture_writes_the_digest_and_each_forks_whole_loop(observer, tmp_path):
    """`debug.enabled` captures, per pass, the digest every fork shared and each
    detector's full loop — input tail, tool calls with their results, and the
    final output. Purely mechanical: strings the worker held anyway."""
    from second_brain.config import GLOBAL, set_value
    from tests.test_fork import _tool_then_silent

    root = tmp_path / "capture"
    set_value("debug.enabled", "true", scope=GLOBAL)
    set_value("debug.path", str(root), scope=GLOBAL)
    observer.provider = FakeProvider({"*": _tool_then_silent()})
    feed("s-obs", text(), tool())
    tick(observer)

    pass_dir = root / "s-obs" / "1"
    digest = (pass_dir / "digest.txt").read_text(encoding="utf-8")
    assert "adding the health trio" in digest          # the episode, verbatim
    assert "cache breakpoint" in digest

    trace = (pass_dir / "default.txt").read_text(encoding="utf-8")
    assert "--- input:" in trace                       # the fork's private tail
    assert "You are the 'default' detector" in trace
    assert "[tool_use] Read" in trace                  # the loop, call by call
    assert "[tool_result Read]" in trace
    assert "--- final output ---" in trace
    assert "verdict: silent" in trace


def test_debug_capture_is_off_by_default(observer):
    from pathlib import Path

    observer.provider = FakeProvider({"*": [_silent()]})
    feed("s-obs", text(), tool())
    tick(observer)
    assert not (Path("/tmp/second-brain") / "s-obs").exists()

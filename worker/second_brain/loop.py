"""The observer loop: continuous, decoupled, and never on the primary's clock.

This is the component every other module serves. It tails the spool, decides when a
pass is due, forks the window across the enabled detectors, merges their conclusions
back, and hands anything worth saying to the gate. It is single-flight per task, and
it degrades to silence for every failure it can have — a provider outage, an
exhausted budget, a detector that will not answer.

Three parts of it are easy to get subtly wrong and are therefore written out here
rather than implied:

- **Two limits that both bind** (§Trigger policy). The clock floor is the throttle
  and binds unconditionally; volume decides only *when* within the band it allows.
  A purely volume-driven trigger turns the primary's most productive burst into a
  pass storm — the moment it is most expensive is the moment the observer would
  decide to run constantly. One trigger is exempt from both: the primary's TURN
  ENDING, which is the last opportunity to give feedback and is infrequent by
  nature — it always fires a pass, and never draws from the salience bucket.
- **Pilot-then-fan-out** (§Warm the cache before fanning out). Firing all N forks
  at once against an unwritten prefix costs N cache *creations* instead of one
  creation and N−1 reads. The pilot is a real detector, because the prefix has to
  be written once regardless.
- **Bursts are absorbed, not chased.** While throttled, observations accumulate and
  coalesce, so the next pass sees a larger episode rather than the queue generating
  more passes — cheaper, and usually the better judgment too.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import fork as forkmod
from . import index, mailbox, paths, spool
from .advice import Advisory
from .config import Config
from .detectors import FEEDBACK_SCHEMA, Detector, enabled as enabled_detectors, pick_pilot, resolve
from .fork import Feedback
from .gate import Gate
from .ledger import Ledger
from .projection import ERROR, META, TOOL, USER, Observation
from .provider import Usage
from .status import Status
from .task import Binding, looks_like_new_goal, new_epoch, on_session_start
from .tools import Toolbox, union_grants
from .window import Window

REQUEST_TTL_S = 300.0
"""How long a `/second-brain-run` request stays live before it is discarded."""

SIGNAL_TTL_S = 300.0
"""How recent a control record must be to change state.

The spool outlives the worker, so a worker that starts without a saved read offset
replays the whole session — and a replayed `session_start` is indistinguishable from
a real one unless age is checked. Observed live: the first worker to start after the
durable-offset fix replayed 8.7 hours of spool, read a stale start signal as genuine,
minted a fresh task, and orphaned the previous ledger. Observations are safe to
replay (they are just text); **signals are not**, because each one moves state."""


_STATEFUL_EVENTS = frozenset({"session_start", "session_end", "stop", "user_prompt"})
"""Control events that move state, and therefore must be fresh to be honoured."""


@dataclass
class DetectorState:
    """The demotion ladder's memory for one detector."""

    timeouts: int = 0
    stride: int = 1
    disabled_until: float = 0.0
    runs: int = 0


@dataclass
class OpenAdvice:
    """A delivered advisory waiting for its originating detector to close it."""

    advice_id: str
    detector: str
    headline: str
    at: float
    observations_since: int = 0


@dataclass
class Budget:
    """Per-task and per-hour token ceilings. Exhaustion means silence, never cheaper advice."""

    task_used: int = 0
    hourly: list[tuple[float, int]] = field(default_factory=list)

    def charge(self, usage: Usage) -> None:
        self.task_used += usage.total
        self.hourly.append((time.time(), usage.total))

    def hour_used(self) -> int:
        cutoff = time.time() - 3600
        self.hourly = [(t, n) for t, n in self.hourly if t >= cutoff]
        return sum(n for _, n in self.hourly)

    def exhausted(self, cfg: Config) -> str:
        if self.task_used >= int(cfg.get("budget.tokens_per_task", 2_000_000)):
            return "task token budget exhausted"
        if self.hour_used() >= int(cfg.get("budget.tokens_per_hour", 600_000)):
            return "hourly token budget exhausted"
        return ""

    def pressure(self, cfg: Config) -> float:
        """0.0 → 1.0 of the hourly ceiling, for budget-aware backoff."""
        ceiling = max(1, int(cfg.get("budget.tokens_per_hour", 600_000)))
        return min(1.0, self.hour_used() / ceiling)


class Observer:
    """One session's observer loop."""

    def __init__(self, session_id: str, cwd: str, workspace: str, *,
                 emit: Callable[[str], None], log: Any, hosted_by: str = "monitor") -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.workspace = workspace
        self.emit = emit
        self.log = log
        self.hosted_by = hosted_by

        self.cfg = Config.load(workspace)
        self.binding = Binding.load(session_id)
        self.binding.workspace = workspace
        self.binding.cwd = cwd
        if not self.binding.task_id:
            self.binding, _ = on_session_start(self.binding, "startup",
                                               workspace=workspace, cwd=cwd)
        self.ledger = Ledger.load(self.binding.task_id, workspace)
        self.window = Window(self.cfg, self.ledger, cwd)
        self.gate = Gate(self.cfg, self.ledger, task_id=self.binding.task_id,
                         session_id=session_id, workspace=workspace, log=log)
        self.toolbox = Toolbox(cwd)
        self.mcp: Any = None
        self.provider: Any = None
        self.status = Status(session_id=session_id, task_id=self.binding.task_id,
                             workspace=workspace, cwd=cwd, hosted_by=hosted_by,
                             model=str(self.cfg.get("model.name")))

        self.reader = spool.SpoolReader(session_id)
        self.episode: list[Observation] = []
        self.pending_chars = 0
        self.pass_number = 0
        self.last_pass_start = 0.0
        self.salience_bucket: list[float] = []
        self.salience_pending = ""
        self.detector_state: dict[str, DetectorState] = {}
        self.open_advice: dict[str, OpenAdvice] = {}
        self.touched: list[str] = []
        self.budget = Budget()
        self.turn_stopped = False
        self.turn_end_pending = False
        self.forced = False
        self.background: dict[str, float] = {}
        self.finished = False
        self.last_index_publish = 0.0
        self.last_index_compact = 0.0
        self.last_sweep = 0.0
        self.last_status = 0.0

    # ── the main loop ───────────────────────────────────────────────────────
    async def run(self) -> None:
        self.log.info("observer attached: session=%s task=%s workspace=%s",
                      self.session_id, self.binding.task_id, self.workspace)
        dropped = mailbox.revalidate(self.session_id,
                                     queue_timeout_s=float(self.cfg.get("gate.queue_timeout_s", 1800)))
        if dropped:
            self.log.info("discarded %d mailbox entries older than this worker", dropped)
        await self._start_mcp()
        self.status.state = "watching"
        self.status.save()

        while not self.finished:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:                               # noqa: BLE001
                # Fail open: an observer that crashes must not take anything with it.
                self.log.exception("observer tick failed: %s", exc)
            await asyncio.sleep(float(self.cfg.get("loop.poll_interval_s", 2.0)))

        if self.mcp is not None:
            await self.mcp.close()
        self.status.state = "stopped"
        self.status.save()
        self.ledger.save(int(self.cfg.get("ledger.max_entries", 60)))

    async def _start_mcp(self) -> None:
        """Connect the configured MCP servers once, for every fork to share.

        Only when something actually asks for them: a workspace with no MCP grants
        pays nothing, and a missing SDK is reported rather than silently reducing
        a detector's reach.
        """
        servers = self.cfg.get("mcp.servers") or {}
        wanted = any(
            (spec.get("tools") or []) and any(
                (isinstance(t, dict) and t.get("mcp")) or
                (isinstance(t, str) and t.startswith("mcp__"))
                for t in spec["tools"])
            for spec in self.cfg.group("detectors").values()
            if isinstance(spec, dict) and spec.get("enabled")
        )
        if not servers or not wanted:
            return
        from .mcpclient import McpHub
        self.mcp = McpHub(servers, self.log)
        await self.mcp.start()
        self.toolbox.mcp = self.mcp
        self.status.mcp = self.mcp.status()
        if not self.mcp.available:
            self.status.note = self.mcp.reason

    async def _tick(self) -> None:
        self._ingest(self.reader.read())
        await self._maintenance()

        if not self.gate.mute.observing():
            self.status.state = "muted"
            return
        reason = self.budget.exhausted(self.cfg)
        if reason:
            self.status.state = f"silent ({reason})"
            self.status.note = reason
            return

        if self._pass_due():
            await self._run_pass()
        await self._push()

    # ── ingestion ───────────────────────────────────────────────────────────
    def _ingest(self, observations: list[Observation]) -> None:
        for obs in observations:
            if obs.kind == META:
                self._handle_meta(obs)
                continue
            self.episode.append(obs)
            self.pending_chars += obs.kept_chars
            self.status.observe(obs.tool or obs.kind, obs.raw_chars, obs.kept_chars)
            if obs.kind == USER:
                self._on_user_prompt(obs)
            for record in self.open_advice.values():
                record.observations_since += 1
            if obs.kind == ERROR:
                self._mark_salient("error")
            if obs.kind == TOOL:
                self.turn_stopped = False
                if obs.meta.get("background"):
                    self.background[obs.meta.get("tool_use_id", str(obs.ts))] = obs.ts
                for locator in obs.locators:
                    path = locator.split(":")[0]
                    if path and path not in self.touched:
                        self.touched.append(path)
        self._coalesce()

    def _live(self, obs: Observation) -> bool:
        """Whether a record is recent enough to be acted on as a signal.

        A missing timestamp counts as stale: the cost of ignoring a real signal is a
        missed task split or a late finish gate, while the cost of acting on a
        replayed one is a discarded ledger or a worker that exits.
        """
        return bool(obs.ts) and (time.time() - obs.ts) < SIGNAL_TTL_S

    def _handle_meta(self, obs: Observation) -> None:
        event = str(obs.meta.get("event", ""))
        if event in _STATEFUL_EVENTS and not self._live(obs):
            self.log.info("ignoring a replayed %s signal from %ds ago",
                          event, int(time.time() - obs.ts) if obs.ts else -1)
            return
        if event == "session_start":
            source = str(obs.meta.get("source", "startup"))
            previous = self.binding.task_id
            self.binding, note = on_session_start(
                self.binding, source, workspace=self.workspace, cwd=self.cwd)
            if self.binding.task_id != previous:
                self._rebind(note)
        elif event == "user_prompt":
            self._mark_salient("user_prompt")
            self.turn_stopped = False
        elif event == "stop":
            # A turn end is not a salient event drawing from the bucket — it is
            # its own unconditional trigger (§Trigger policy): the last moment
            # feedback on the turn is still cheap, and terminations are
            # infrequent by nature, so neither the clock floor nor the volume
            # threshold applies to the pass it fires.
            self.turn_end_pending = True
            self.turn_stopped = True
        elif event == "subagent_stop":
            self.background.pop(str(obs.meta.get("agent_id", "")), None)
        elif event == "delivered":
            advice_id = str(obs.meta.get("advice_id", ""))
            if advice_id and not obs.meta.get("human_only"):
                self.open_advice[advice_id] = OpenAdvice(
                    advice_id=advice_id, detector=str(obs.meta.get("kind", "")),
                    headline="", at=obs.ts or time.time())
                self.status.advisories_delivered += 1
                self.status.detector(str(obs.meta.get("kind", "")))["delivered"] += 1
            self.turn_stopped = False
        elif event == "session_end":
            self.log.info("session ended; worker exiting")
            self.finished = True

    def _on_user_prompt(self, obs: Observation) -> None:
        """The goal, and the soft task boundary.

        A session routinely carries three unrelated pieces of work, and carrying
        task 1's decisions into task 3's advice is the drift task scoping exists to
        prevent. The split is a cheap structural proxy rather than a model call: a
        false split costs a cold start, a missed split costs an irrelevant prefix.
        """
        if self._live(obs) and looks_like_new_goal(obs.body, time.time() - self.binding.started_at):
            self.binding = new_epoch(self.binding, "the user stated a new goal")
            self._rebind("new goal in the same session")
            # The prompt that split the task is the new task's first observation,
            # so it is put back after the reset rather than lost with the old one.
            self.episode.append(obs)
            self.pending_chars += obs.kept_chars
        if not self.ledger.goal:
            self.ledger.goal = obs.body.strip().splitlines()[0][:160]

    def _rebind(self, note: str) -> None:
        """Switch to a new task: fresh ledger, fresh window, fresh gate."""
        self.log.info("task boundary: %s → %s (%s)", self.status.task_id,
                      self.binding.task_id, note)
        self.ledger.save(int(self.cfg.get("ledger.max_entries", 60)))
        self.ledger = Ledger.load(self.binding.task_id, self.workspace)
        self.window = Window(self.cfg, self.ledger, self.cwd)
        self.gate = Gate(self.cfg, self.ledger, task_id=self.binding.task_id,
                         session_id=self.session_id, workspace=self.workspace, log=self.log)
        self.episode.clear()
        self.pending_chars = 0
        self.open_advice.clear()
        self.touched.clear()
        self.status.task_id = self.binding.task_id

    def _mark_salient(self, kind: str) -> None:
        """A salient event may fire a pass early, drawing from a bounded bucket."""
        if kind not in (self.cfg.get("loop.salience_triggers") or []):
            return
        now = time.time()
        self.salience_bucket = [t for t in self.salience_bucket if now - t < 3600]
        if len(self.salience_bucket) < int(self.cfg.get("loop.salience_per_hour", 6)):
            self.salience_bucket.append(now)
            self.salience_pending = kind

    def _coalesce(self) -> None:
        """Keep the episode bounded: drop the least salient middles, never the ends.

        The observer is allowed to miss detail; it is never allowed to lag
        unboundedly behind.
        """
        cap = int(self.cfg.get("loop.episode_cap_chars", 60000))
        if self.pending_chars <= cap:
            return
        # Keep the opening (what the episode set out to do), every salient event
        # (errors and user prompts), and as much of the recent tail as fits.
        keep: set[int] = set(range(min(5, len(self.episode))))
        budget = cap
        for i in list(keep):
            budget -= self.episode[i].kept_chars
        for i, obs in enumerate(self.episode):
            if obs.salient and i not in keep:
                keep.add(i)
                budget -= obs.kept_chars
        for i in range(len(self.episode) - 1, -1, -1):
            if i in keep:
                continue
            size = self.episode[i].kept_chars
            if size > budget:
                break
            keep.add(i)
            budget -= size

        dropped = len(self.episode) - len(keep)
        self.episode = [o for i, o in enumerate(self.episode) if i in keep]
        self.pending_chars = sum(o.kept_chars for o in self.episode)
        if dropped > 0:
            self.log.info("coalesced episode: dropped %d low-salience observations", dropped)

    # ── trigger policy ──────────────────────────────────────────────────────
    def _requested(self) -> bool:
        """Did a human ask for a pass now? Consumed on read, so it fires once.

        A request **expires**. The worker is a long-lived process that holds the code
        it imported at startup, so a request written while an older worker was running
        is never consumed — and would otherwise fire a surprising pass whenever the
        next worker started. Asking for a pass is a live request, not a queued job.
        """
        path = paths.trigger_path(self.session_id)
        try:
            age = time.time() - path.stat().st_mtime
            path.unlink()
        except OSError:
            return False
        if age > REQUEST_TTL_S:
            self.log.info("ignoring a pass request from %ds ago", int(age))
            return False
        return True

    def _pass_due(self) -> bool:
        # An explicit request bypasses the clock floor and the volume threshold —
        # it is the one trigger a person controls, and making them wait out
        # `min_interval_s` would defeat the point of asking. It does not bypass the
        # mute or the budget, which are the limits that mean something.
        if self._requested():
            self.forced = True
            return True
        if self.turn_end_pending and self.episode:
            # The primary's loop ended: the last opportunity to give feedback on
            # the turn, and the one trigger exempt from BOTH cadence limits —
            # no clock floor, no volume threshold, no salience bucket. Turn
            # terminations are infrequent by nature, so the exemption cannot
            # become a pass storm; mute and budget still bind (checked before
            # this in the tick), and an empty episode still means there is
            # nothing to judge.
            return True
        if not self.episode:
            return False
        now = time.time()
        since = now - self.last_pass_start
        floor = float(self.cfg.get("loop.min_interval_s", 90))
        if self.cfg.get("loop.backoff_on_budget", True):
            # Stretch the throttle as the budget depletes rather than running full
            # speed into a wall and going abruptly silent.
            floor *= 1.0 + 3.0 * self.budget.pressure(self.cfg) ** 2
        if since < floor:
            return False
        if self.salience_pending:
            return True
        if self.pending_chars >= int(self.cfg.get("loop.trigger_chars", 6000)):
            return True
        return since >= float(self.cfg.get("loop.max_interval_s", 900))

    # ── one pass ────────────────────────────────────────────────────────────
    async def _run_pass(self) -> None:
        # Picked up at a pass boundary — and *propagated*. Rebinding this attribute
        # alone leaves the gate and the window holding the previous Config object, so
        # every `gate.*` and `window.*` knob would silently never take effect on a
        # running worker while `/second-brain-config` reported it as set.
        self.cfg = Config.load(self.workspace)
        self.gate.cfg = self.cfg
        self.window.cfg = self.cfg
        self.last_pass_start = time.time()
        self.pass_number += 1
        trigger = ("requested" if self.forced
                   else "turn_end" if self.turn_end_pending
                   else self.salience_pending or "volume")
        self.forced = False
        self.turn_end_pending = False
        self.salience_pending = ""
        episode, self.episode = self.episode, []
        self.pending_chars = 0

        # Staleness, checked before anything is generated: the primary has been
        # working while the mailbox waited.
        self.gate.revalidate(episode)
        self.gate.expire_sweep()

        # The episode joins the window BEFORE the snapshot, so every fork's
        # cacheable prefix includes this pass's input. In the tail it would sit
        # after the breakpoint and the shared prefix would lag one episode behind
        # the window — enough to keep a short session below the provider's
        # minimum cacheable length for its whole life.
        episode_chars = self.window.append_episode(episode, self.pass_number)

        if self.provider is None and not self._connect():
            return

        # Computed once per pass: it costs a git call and two detectors ask for it.
        structural = self._structural_evidence()
        detectors = self._due_detectors(structural)
        if not detectors:
            return
        if structural and any(d.structural for d in detectors):
            self.window.append_note(structural)

        self.status.state = "thinking"
        self.status.save()
        snapshot = self.window.snapshot()
        started = time.monotonic()
        capture = self._capture_dir()
        if capture is not None:
            self._capture(capture / "digest.txt",
                          self.window.render_digest(session_id=self.session_id,
                                                    task_id=self.binding.task_id,
                                                    pass_number=self.pass_number))

        results = await self._fan_out(detectors, snapshot, has_new=episode_chars > 0)
        if capture is not None:
            for feedback in results:
                self._capture(capture / f"{paths.safe_name(feedback.detector)}.txt",
                              feedback.trace + "\n\n--- final output ---\n"
                              + feedback.dump() + "\n")

        # Only the loop writes to the window, after the fan-out returns, in
        # detector-name order — not completion order, so a replay of the same
        # session produces the same window and the next prefix is stable.
        results.sort(key=lambda f: f.detector)
        lines = [f.line(self.pass_number) for f in results]
        self.window.append_feedback(lines, self.pass_number)
        # The same lines the window gets, kept where a human surface can read them
        # without a running worker to ask (`/second-brain-run`, `/second-brain-why`).
        self.status.last_feedback = lines

        self._absorb(results, trigger)
        if trigger == "turn_end":
            # The one-line outcome for the statusline: visible seconds after the
            # turn ends with NO next interaction required — the detailed report
            # below still needs one, because hooks only speak when invoked.
            advised = [f.detector for f in results if f.verdict == "advise"]
            self.status.turn_verdict = (", ".join(f"{d} advised" for d in advised)
                                        or "all silent")
            self.status.turn_verdict_at = time.time()
        if trigger == "turn_end" and self.cfg.get("loop.turn_end_report", True):
            # The user asked to SEE that the turn-end look happened, verdicts and
            # all — but the model must not: the drain hook renders this as
            # `systemMessage` only. Claim-once, so it shows exactly one time.
            try:
                paths.write_private(paths.turn_report_path(self.session_id),
                                    json.dumps({"at": time.time(), "pass": self.pass_number,
                                                "lines": lines}))
            except OSError as exc:
                self.log.warning("turn report write failed: %s", exc)
        if self.window.needs_compaction():
            await self.window.compact(self.provider, self.log)
        self._save_status(time.monotonic() - started, trigger)

    def _capture_dir(self) -> Path | None:
        """Where this pass's debug capture lands, or None when capture is off.

        `<debug.path>/<session>/<pass>/` — digest.txt is the shared prefix every
        fork received; `<detector>.txt` is that fork's whole loop. Purely
        mechanical: the files are strings the worker holds anyway, and no model
        is involved in producing them.
        """
        if not self.cfg.get("debug.enabled", False):
            return None
        root = str(self.cfg.get("debug.path", "") or "/tmp/second-brain")
        return Path(root) / paths.safe_name(self.session_id) / str(self.pass_number)

    def _capture(self, path: Path, text: str) -> None:
        try:
            paths.write_private(path, text)
        except OSError as exc:
            self.log.warning("debug capture failed for %s: %s", path, exc)

    def _connect(self) -> bool:
        from .provider import ProviderError, make_provider
        try:
            self.provider = make_provider(self.cfg)
            return True
        except ProviderError as exc:
            self.status.state = f"silent ({exc})"
            self.status.note = str(exc)
            self.status.save()
            self.log.warning("no provider: %s", exc)
            return False

    def _due_detectors(self, structural: str = "") -> list[Detector]:
        now = time.time()
        out: list[Detector] = []
        for detector in enabled_detectors(resolve(self.cfg.group("detectors"))):
            state = self.detector_state.setdefault(detector.name, DetectorState())
            if state.disabled_until and now < state.disabled_until:
                continue
            if state.disabled_until and now >= state.disabled_until:
                # Recovery: one retry, in case the cause was transient.
                self.log.info("re-enabling %s after its demotion window", detector.name)
                state.disabled_until = 0.0
                state.timeouts = 0
                state.stride = 1
                self.status.detector(detector.name)["state"] = "retrying"
            if detector.structural and not structural:
                # A structurally-triggered detector runs only when the worker has
                # actually computed a match, so it cannot invent one.
                continue
            if detector.due(self.pass_number, state.stride):
                out.append(detector)
        return out

    async def _fan_out(self, detectors: list[Detector], snapshot: Any,
                       has_new: bool) -> list[Feedback]:
        """Pilot first and alone, then the rest in parallel against a warm prefix."""
        deadline = float(self.cfg.get("loop.fork_deadline_s", 20))
        grace = float(self.cfg.get("loop.fork_grace_s", 8))
        width = int(self.cfg.get("loop.max_parallel_forks", 6))
        semaphore = asyncio.Semaphore(max(1, width))
        # One wire-level tools list for the whole pass. Tools precede system and
        # messages in the provider's cache key, so a per-fork tools array would
        # give every fork a different prefix and no cache write would ever be
        # read. Access stays per-detector: dispatch re-checks each call against
        # the calling fork's grant, and each fork's tail names what it may use.
        pass_tools = [*self.toolbox.definitions(union_grants(d.grant for d in detectors)),
                      FEEDBACK_SCHEMA]

        async def one(detector: Detector) -> Feedback:
            async with semaphore:
                soft = detector.deadline_s or deadline
                return await forkmod.run_with_deadline(
                    detector, snapshot, hard_deadline_s=soft + grace,
                    provider=self.provider, toolbox=self.toolbox,
                    tools=pass_tools, has_new=has_new,
                    open_advice=self._open_for(detector.name), deadline_s=soft,
                    body_cap=int(self.cfg.get("gate.body_cap", 700)),
                    max_iterations=int(self.cfg.get("loop.max_fork_iterations", 6)),
                    max_output_tokens=int(self.cfg.get("budget.max_output_tokens", 1024)),
                    log=self.log,
                )

        pilot = pick_pilot(detectors)
        results: list[Feedback] = []
        rest = [d for d in detectors if d is not pilot]
        if pilot is not None:
            results.append(await one(pilot))
        if rest:
            results.extend(await asyncio.gather(*(one(d) for d in rest)))
        return results

    def _open_for(self, detector: str) -> list[dict[str, Any]]:
        """Outstanding advisories this detector must adjudicate in its next fork."""
        return [{"id": r.advice_id, "headline": r.headline or "(see your last advisory)",
                 "at": r.at} for r in self.open_advice.values() if r.detector == detector]

    # ── absorbing a pass ────────────────────────────────────────────────────
    def _absorb(self, results: list[Feedback], trigger: str) -> None:
        resolved: set[str] = set()
        for feedback in results:
            stats = self.status.detector(feedback.detector)
            stats["runs"] += 1
            stats["cache_read"] = int(stats.get("cache_read", 0)) + feedback.usage.cache_read
            stats["cache_write"] = int(stats.get("cache_write", 0)) + feedback.usage.cache_write
            self.budget.charge(feedback.usage)
            self._charge_status(feedback.usage)
            self._demotion(feedback)

            for outcome in feedback.outcomes:
                self._close_outcome(outcome)
            if feedback.verdict == "resolved" and feedback.dedup_key:
                resolved.add(feedback.dedup_key)
            if feedback.verdict != "advise":
                continue

            stats["advised"] += 1
            self.status.advisories_generated += 1
            advisory = self.gate.consider(feedback)
            if advisory is None:
                self.status.advisories_dropped += 1
                continue
            if advisory.finish_gate and not self._genuinely_finished():
                # Conservative by construction: any doubt about outstanding work
                # means the finish gate stays out of it.
                advisory.finish_gate = False
            self.gate.enqueue(advisory)

        if resolved:
            self.gate.resolve(resolved)
        self._expire_outcomes()
        self.ledger.save(int(self.cfg.get("ledger.max_entries", 60)))

    def _charge_status(self, usage: Usage) -> None:
        self.status.tokens["input"] += usage.input_tokens
        self.status.tokens["output"] += usage.output_tokens
        self.status.tokens["cache_read"] += usage.cache_read
        self.status.tokens["cache_write"] += usage.cache_write
        self.status.budget_task_used = self.budget.task_used
        self.status.budget_hour_used = self.budget.hour_used()

    def _demotion(self, feedback: Feedback) -> None:
        """Rate-limit, then disable, then retry — a detector that cannot keep up.

        Repeated timeouts are not a transient annoyance: that detector burns a
        fork's worth of tokens every pass, produces nothing, and holds the pass open
        until the deadline. Nothing here is silent.
        """
        state = self.detector_state.setdefault(feedback.detector, DetectorState())
        stats = self.status.detector(feedback.detector)
        if feedback.error:
            stats["errors"] += 1
        if not feedback.timed_out:
            state.timeouts = 0
            if state.stride > 1:
                state.stride = 1
                stats["state"] = "active"
                self.log.info("%s is keeping up again; back to every pass", feedback.detector)
            return

        state.timeouts += 1
        stats["timeouts"] += 1
        if state.timeouts >= int(self.cfg.get("loop.disable_after_timeouts", 4)):
            state.disabled_until = time.time() + float(self.cfg.get("loop.demote_retry_s", 1800))
            stats["state"] = "disabled (timeouts)"
            self.log.warning("%s disabled for this task after %d timeouts; retrying in %ds",
                             feedback.detector, state.timeouts,
                             int(self.cfg.get("loop.demote_retry_s", 1800)))
        elif state.timeouts >= int(self.cfg.get("loop.demote_after_timeouts", 2)):
            state.stride = int(self.cfg.get("loop.demote_stride", 3))
            stats["state"] = f"demoted (every {state.stride}th pass)"
            self.log.info("%s demoted to every %dth pass after %d timeouts",
                          feedback.detector, state.stride, state.timeouts)

    def _close_outcome(self, outcome: forkmod.Outcome) -> None:
        record = self.open_advice.pop(outcome.advice_id, None)
        if record is None:
            return
        verdict = outcome.verdict if outcome.verdict in {
            "adopted", "partially_adopted", "rejected", "already_handled",
            "no_evidence", "contradicted"} else "no_evidence"
        # Evidence or nothing: a model grading its own advice is structurally
        # flattering, so ambiguity resolves against the observer.
        if verdict != "no_evidence" and not outcome.evidence:
            verdict = "no_evidence"
        self.ledger.close_advice(outcome.advice_id, verdict, outcome.evidence)
        stats = self.status.detector(record.detector)
        if verdict in {"adopted", "partially_adopted"}:
            stats["adopted"] += 1

    def _expire_outcomes(self) -> None:
        """An open record self-closes as no_evidence rather than piling up."""
        max_obs = int(self.cfg.get("adjudication.window_observations", 60))
        max_age = float(self.cfg.get("adjudication.window_seconds", 1800))
        now = time.time()
        for advice_id, record in list(self.open_advice.items()):
            if record.observations_since >= max_obs or now - record.at > max_age:
                self.ledger.close_advice(advice_id, "no_evidence", [])
                self.open_advice.pop(advice_id, None)

    # ── the finish gate's precondition ──────────────────────────────────────
    def _genuinely_finished(self) -> bool:
        """Did the primary really stop, with nothing outstanding?

        The `Stop` payload does not say so, so it is inferred from what was
        observed: background commands and subagents launched and never reported.
        Any doubt means silence — resuming an agent that is actually still working
        is both useless and confusing.
        """
        if not self.turn_stopped:
            return False
        settle = float(self.cfg.get("finish_gate.background_settle_s", 600))
        now = time.time()
        self.background = {k: t for k, t in self.background.items() if now - t < settle}
        return not self.background

    def _structural_evidence(self) -> str:
        """Cross-task collisions: computed by the worker, never judged by the model."""
        if not self.cfg.get("index.enabled", True) or not self.touched:
            return ""
        if not any(spec.get("enabled") and spec.get("structural")
                   for spec in self.cfg.group("detectors").values() if isinstance(spec, dict)):
            return ""       # nobody would read it, and it costs a git call to build
        found = index.collisions(
            self.workspace, task=self.binding.task_id, cwd=self.cwd,
            git_dir=index.git_common_dir(self.cwd), touched=self.touched,
            ttl_s=float(self.cfg.get("index.ttl_s", 900)),
        )
        if not found:
            return ""
        return ("=== structural finding: another live task shares these paths ===\n"
                + "\n".join(index.describe(c) for c in found[:3]))

    # ── delivery, maintenance, status ───────────────────────────────────────
    async def _push(self) -> None:
        """The monitor channel: claim one advisory and push it as a notification.

        Only under the monitor host — a hook-spawned worker's stdout is a log file,
        so pushing there would deliver to nobody. Finish-gate advisories are left
        for the `Stop` hook unless the turn has already ended, which is the deferred
        wake the finish gate needs. Human-only advice is never pushed here: a
        monitor line is delivered to the agent as a notification, and human-only
        means it must NOT enter the model's context — the drain hook's
        `systemMessage` is the only channel that can reach the person alone.
        """
        if self.hosted_by != "monitor":
            return
        deferred_ok = self._genuinely_finished()
        advisory = mailbox.claim(
            self.session_id,
            queue_timeout_s=float(self.cfg.get("gate.queue_timeout_s", 1800)),
            predicate=lambda a: (not a.human_only) and ((not a.finish_gate) or deferred_ok),
        )
        if advisory is None:
            return
        headline_cap = int(self.cfg.get("gate.headline_cap", 160))
        body_cap = int(self.cfg.get("gate.body_cap", 700))
        # One line carries both addressees: the agent's framed copy and the
        # user-visible attribution, so they cannot diverge or arrive apart.
        text = advisory.for_agent(headline_cap, body_cap) + "\n\n" + advisory.for_user(
            headline_cap, body_cap)
        self.emit(text.replace("\n", " ⏎ "))
        self._observe_delivery(advisory)

    def _observe_delivery(self, advisory: Advisory) -> None:
        spool.append(self.session_id, [Observation(
            kind=META, ts=time.time(), body="delivered",
            meta={"event": "delivered", "advice_id": advisory.id, "kind": advisory.kind,
                  "dedup_key": advisory.dedup_key, "channel": "monitor",
                  "human_only": advisory.human_only},
        )])

    async def _maintenance(self) -> None:
        now = time.time()
        if now - self.last_index_publish > 30 and self.cfg.get("index.enabled", True):
            self.last_index_publish = now
            index.publish(self.workspace, task=self.binding.task_id, cwd=self.cwd,
                          git_dir=index.git_common_dir(self.cwd), goal=self.ledger.goal,
                          touched=self.touched,
                          max_paths=int(self.cfg.get("index.max_paths_per_entry", 40)))
        if now - self.last_index_compact > float(self.cfg.get("index.compact_interval_s", 600)):
            self.last_index_compact = now
            index.compact(self.workspace, ttl_s=float(self.cfg.get("index.ttl_s", 900)))
        if now - self.last_sweep > float(self.cfg.get("ledger.sweep_interval_s", 3600)):
            self.last_sweep = now
            self._sweep()
        if now - self.last_status > float(self.cfg.get("loop.status_interval_s", 30)):
            self._save_status(0.0, "")

    def _sweep(self) -> None:
        from .ledger import sweep
        removed = sweep(
            ttl_days=float(self.cfg.get("ledger.ttl_days", 7)),
            max_per_workspace=int(self.cfg.get("ledger.max_per_workspace", 50)),
            max_bytes=int(self.cfg.get("ledger.max_bytes", 8_000_000)),
            active={self.binding.task_id},
        )
        if removed:
            self.log.info("ledger sweep removed %d expired ledgers: %s",
                          len(removed), ", ".join(removed[:8]))

    def _save_status(self, elapsed_s: float, trigger: str) -> None:
        self.last_status = time.time()
        if elapsed_s:
            self.status.passes = self.pass_number
            self.status.last_pass_at = self.last_pass_start
            self.status.last_pass_s = round(elapsed_s, 2)
            self.status.note = f"triggered by {trigger}" if trigger else self.status.note
        self.status.pending_chars = self.pending_chars
        self.status.window_chars = self.window.chars
        self.status.window_fill = round(self.window.fill, 3)
        self.status.compactions = self.window.compactions
        self.status.state = "watching" if self.gate.mute.observing() else "muted"
        for name, state in self.detector_state.items():
            uptake = self.ledger.uptake(name)
            if uptake is not None:
                self.status.detector(name)["uptake"] = round(uptake, 2)
            self.status.detector(name)["stride"] = state.stride
        self.status.save()

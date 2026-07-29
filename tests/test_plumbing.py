"""Plumbing: hooks, spool, transcript offsets, mailbox, window, task identity, index.

These are the parts that must work before a token is ever spent, and the ones whose
failure the design promises is invisible — so most of what is asserted here is that
nothing escapes: hooks exit 0 and print nothing on the feed path, an unenrolled
workspace is never read, a mailbox entry is delivered exactly once.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from second_brain import index, mailbox, paths, spool, transcript
from second_brain.advice import Advisory
from second_brain.config import Config, ConfigError, coerce, set_value
from second_brain.ledger import Ledger, forget, sweep
from second_brain.projection import TEXT, Observation
from second_brain.task import Binding, looks_like_new_goal, on_session_start
from second_brain.window import Window

ROOT = Path(__file__).resolve().parent.parent


# ── the manifests, against the host's actual schemas ────────────────────────
def test_the_monitor_manifest_matches_what_the_host_parses():
    """`monitors/monitors.json` is a bare ARRAY of strict objects.

    Verified by reading the installed CLI (v2.1.220): the loader parses the file
    with `z.array(monitorSchema)`, and the entry schema is a **strictObject** of
    `name` / `command` / `description` (all required) plus an optional `when`. An
    object wrapper or one extra key fails the whole plugin's monitor load — and
    the worker is hosted by that monitor, so it fails silently and completely.
    """
    monitors = json.loads((ROOT / "monitors" / "monitors.json").read_text())
    assert isinstance(monitors, list) and monitors
    assert len({m["name"] for m in monitors}) == len(monitors)   # unique within the plugin
    for monitor in monitors:
        assert set(monitor) <= {"name", "command", "description", "when"}
        assert {"name", "command", "description"} <= set(monitor)
        assert all(monitor[k].strip() for k in ("name", "command", "description"))
        assert monitor.get("when", "always") == "always" or \
            monitor["when"].startswith("on-skill-invoke:")
        # `${user_config.*}` in a monitor command is refused by the host: the
        # substituted value would reach a shell.
        assert "${user_config" not in monitor["command"]


def test_hook_and_plugin_manifests_point_at_files_that_exist():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    referenced = [
        entry["command"]
        for matchers in hooks.values() for matcher in matchers
        for entry in matcher["hooks"]
    ]
    assert referenced
    for command in referenced:
        assert command.startswith("python3 \"${CLAUDE_PLUGIN_ROOT}/")
        script = command.split('${CLAUDE_PLUGIN_ROOT}/')[1].split('"')[0]
        assert (ROOT / script).exists(), script

    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["name"] == "second-brain"
    for command in (ROOT / "commands").glob("*.md"):
        body = command.read_text()
        assert "${CLAUDE_PLUGIN_ROOT}/commands/sb.py" in body


# ── the hooks, as processes ─────────────────────────────────────────────────
def run_hook(script: str, mode: str, payload: dict, env_extra: dict | None = None):
    env = {**os.environ, "SECOND_BRAIN_NO_SPAWN": "1"}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / script), mode],
        input=json.dumps(payload), capture_output=True, text=True, env=env, check=False,
    )


def write_transcript(path: Path, records: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def assistant_record(text: str, cwd: str = "/repo") -> dict:
    return {"type": "assistant", "cwd": cwd, "timestamp": "2026-07-29T10:00:00.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def test_feed_hook_spools_and_says_nothing(tmp_path):
    transcript_path = tmp_path / "session.jsonl"
    write_transcript(transcript_path, [assistant_record("adding the health trio")])
    result = run_hook("feed.py", "post_tool", {
        "session_id": "s-1", "cwd": str(tmp_path), "transcript_path": str(transcript_path),
        "tool_name": "Write",
    })
    assert result.returncode == 0
    assert result.stdout == ""                      # the feed path never speaks
    observations = spool.SpoolReader("s-1").read()
    assert [o.kind for o in observations] == [TEXT]
    assert observations[0].body == "adding the health trio"


def test_feed_hook_reads_forward_and_never_repeats(tmp_path):
    transcript_path = tmp_path / "session.jsonl"
    payload = {"session_id": "s-2", "cwd": str(tmp_path), "transcript_path": str(transcript_path)}
    write_transcript(transcript_path, [assistant_record("first")])
    run_hook("feed.py", "post_tool", payload)
    write_transcript(transcript_path, [assistant_record("second")])
    run_hook("feed.py", "post_tool", payload)
    bodies = [o.body for o in spool.SpoolReader("s-2").read() if o.kind == TEXT]
    assert bodies == ["first", "second"]


def test_an_unenrolled_workspace_is_never_read(tmp_path):
    workspace = paths.workspace_key(str(tmp_path))
    set_value("enable.default", "false", scope="global")
    transcript_path = tmp_path / "session.jsonl"
    write_transcript(transcript_path, [assistant_record("secret work")])
    run_hook("feed.py", "post_tool", {"session_id": "s-3", "cwd": str(tmp_path),
                                      "transcript_path": str(transcript_path)})
    assert not paths.spool_path("s-3").exists()
    assert Config.load(workspace).observing(workspace) is False


def test_subagent_stop_keeps_the_conclusion_not_the_conversation(tmp_path):
    result = run_hook("feed.py", "subagent_stop", {
        "session_id": "s-4", "cwd": str(tmp_path), "transcript_path": "",
        "agent_id": "a1", "agent_type": "Explore",
        "last_assistant_message": "Found three call sites: a.go:1, b.go:2, c.go:3",
    })
    assert result.returncode == 0
    kinds = [(o.kind, o.tool) for o in spool.SpoolReader("s-4").read()]
    assert ("subagent", "Explore") in kinds


def test_a_malformed_payload_still_exits_zero(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "hooks" / "feed.py"), "post_tool"],
                            input="not json", capture_output=True, text=True,
                            env={**os.environ, "SECOND_BRAIN_NO_SPAWN": "1"}, check=False)
    assert result.returncode == 0 and result.stdout == ""


def test_drain_hook_addresses_the_agent_and_the_user_together(tmp_path):
    advisory = Advisory(task_id="t", session_id="s-5", workspace=str(tmp_path), kind="git-log",
                        headline="this file was rewritten two days ago in e8ac6d7",
                        confidence=0.8, evidence=["e8ac6d7"])
    mailbox.put("s-5", advisory)
    result = run_hook("drain.py", "tool", {"session_id": "s-5", "cwd": str(tmp_path)})
    assert result.returncode == 0
    emitted = json.loads(result.stdout)
    assert emitted["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert advisory.headline in emitted["hookSpecificOutput"]["additionalContext"]
    assert advisory.headline in emitted["systemMessage"]     # never one without the other


def test_drain_delivers_exactly_once(tmp_path):
    mailbox.put("s-6", Advisory(task_id="t", session_id="s-6", workspace="/w",
                                kind="default", headline="only once", confidence=0.9))
    first = run_hook("drain.py", "tool", {"session_id": "s-6", "cwd": str(tmp_path)})
    second = run_hook("drain.py", "tool", {"session_id": "s-6", "cwd": str(tmp_path)})
    assert "only once" in first.stdout
    assert second.stdout == ""


def test_sub_threshold_advice_reaches_the_user_only(tmp_path):
    mailbox.put("s-7", Advisory(task_id="t", session_id="s-7", workspace="/w", kind="default",
                                headline="a hunch", confidence=0.4, human_only=True))
    result = run_hook("drain.py", "tool", {"session_id": "s-7", "cwd": str(tmp_path)})
    emitted = json.loads(result.stdout)
    assert "hookSpecificOutput" not in emitted        # the model's context is untouched
    assert "a hunch" in emitted["systemMessage"]


def test_the_finish_gate_continues_a_turn_at_most_once_per_window(tmp_path):
    def enqueue():
        mailbox.put("s-8", Advisory(task_id="t-8", session_id="s-8", workspace="/w",
                                    kind="standard-questions", finish_gate=True, confidence=0.9,
                                    headline="version bumped, no deploy command observed"))
    enqueue()
    first = json.loads(run_hook("drain.py", "stop",
                                {"session_id": "s-8", "cwd": str(tmp_path)}).stdout)
    assert first["decision"] == "block"
    assert "no deploy command observed" in first["reason"]
    assert "systemMessage" in first                  # a resumed turn is never mysterious

    enqueue()
    second = json.loads(run_hook("drain.py", "stop",
                                 {"session_id": "s-8", "cwd": str(tmp_path)}).stdout)
    assert "decision" not in second
    assert "held back" in second["systemMessage"]


def test_the_finish_gate_never_chains_onto_its_own_continuation(tmp_path):
    mailbox.put("s-9", Advisory(task_id="t-9", session_id="s-9", workspace="/w", kind="d",
                                finish_gate=True, confidence=0.99, headline="more to do"))
    result = run_hook("drain.py", "stop", {"session_id": "s-9", "cwd": str(tmp_path),
                                           "stop_hook_active": True})
    assert result.stdout == ""


def test_normal_advice_is_not_taken_by_the_stop_hook(tmp_path):
    mailbox.put("s-10", Advisory(task_id="t", session_id="s-10", workspace="/w", kind="d",
                                 confidence=0.9, headline="ordinary finding"))
    assert run_hook("drain.py", "stop", {"session_id": "s-10", "cwd": str(tmp_path)}).stdout == ""
    assert "ordinary finding" in run_hook("drain.py", "tool",
                                          {"session_id": "s-10", "cwd": str(tmp_path)}).stdout


# ── transcript cursor ───────────────────────────────────────────────────────
def test_a_shrunken_transcript_reseeks_instead_of_reprojecting(tmp_path):
    path = tmp_path / "t.jsonl"
    write_transcript(path, [assistant_record("one"), assistant_record("two")])
    assert len(transcript.read_new_records(path, "s-x")) == 2
    path.write_text("")                              # a different session at the same path
    assert transcript.read_new_records(path, "s-x") == []
    write_transcript(path, [assistant_record("three")])
    assert len(transcript.read_new_records(path, "s-x")) == 1


def test_a_partial_last_line_is_left_for_the_next_read(tmp_path):
    path = tmp_path / "t.jsonl"
    write_transcript(path, [assistant_record("whole")])
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "assistant", "mess')
    assert len(transcript.read_new_records(path, "s-y")) == 1
    with path.open("a", encoding="utf-8") as fh:
        fh.write('age": {"role": "assistant", "content": []}}\n')
    assert len(transcript.read_new_records(path, "s-y")) == 1


# ── window discipline ───────────────────────────────────────────────────────
def test_the_window_only_ever_grows_at_the_end(cfg):
    window = Window(cfg, Ledger(task_id="t"), "/repo")
    before = window.snapshot()
    window.append_episode([Observation(kind=TEXT, body="new work")], 2)
    after = window.snapshot()
    assert after.blocks[:len(before.blocks)] == before.blocks
    assert len(after.blocks) == len(before.blocks) + 1


def test_compaction_distils_into_the_ledger_and_lowers_the_fill(cfg):
    cfg.values["window"]["budget_chars"] = 4000
    ledger = Ledger(task_id="t-compact")
    window = Window(cfg, ledger, "/repo")
    for i in range(20):
        window.append_episode([Observation(kind=TEXT, body="x" * 400)], i)
    assert window.needs_compaction()

    class Summarizer:
        async def send(self, **kwargs):
            from second_brain.provider import Reply
            return Reply(text="- edited health.go\n- ran no tests")

    async def go():
        return await window.compact(Summarizer(), None)

    assert asyncio.run(go()) is True
    assert window.fill < cfg.get("window.compaction_threshold")
    assert any("health.go" in e["text"] for e in ledger.entries)


def test_a_failed_summary_is_recorded_rather_than_hidden(cfg):
    cfg.values["window"]["budget_chars"] = 2000
    ledger = Ledger(task_id="t-gap")
    window = Window(cfg, ledger, "/repo")
    for i in range(10):
        window.append_episode([Observation(kind=TEXT, body="y" * 400)], i)

    class Broken:
        async def send(self, **kwargs):
            raise RuntimeError("provider down")

    asyncio.run(window.compact(Broken(), None))
    assert any(e["kind"] == "compaction-gap" for e in ledger.entries)


# ── task identity ───────────────────────────────────────────────────────────
def test_startup_and_clear_mint_a_new_task():
    binding = Binding(session_id="s")
    binding, _ = on_session_start(binding, "startup", workspace="/w", cwd="/w")
    first = binding.task_id
    binding, _ = on_session_start(binding, "clear", workspace="/w", cwd="/w")
    assert binding.task_id != first


def test_resume_and_compact_continue_the_same_task():
    binding = Binding(session_id="s")
    binding, _ = on_session_start(binding, "startup", workspace="/w", cwd="/w")
    task = binding.task_id
    for source in ("resume", "compact"):
        binding, _ = on_session_start(binding, source, workspace="/w", cwd="/w")
        assert binding.task_id == task


def test_fork_copies_the_ledger_then_diverges():
    binding = Binding(session_id="s")
    binding, _ = on_session_start(binding, "startup", workspace="/w", cwd="/w")
    ledger = Ledger.load(binding.task_id, "/w")
    ledger.add_entry("decided to use the shared health package")
    ledger.save()

    binding, note = on_session_start(binding, "fork", workspace="/w", cwd="/w")
    assert note == "forked task"
    copy = Ledger.load(binding.task_id, "/w")
    assert any("shared health package" in e["text"] for e in copy.entries)
    copy.add_entry("only on this branch")
    copy.save()
    assert len(Ledger.load(ledger.task_id, "/w").entries) == 1


@pytest.mark.parametrize("prompt,expected", [
    ("now let's do something completely different with the deploy pipeline", True),
    ("also fix the typo in the comment above it", False),
    ("why?", False),
    ("continue", False),
])
def test_the_soft_task_split_is_cheap_and_wrong_in_the_cheap_direction(prompt, expected):
    assert looks_like_new_goal(prompt, 3600) is expected


def test_a_young_task_is_never_split():
    assert looks_like_new_goal("now let's do something completely different here", 10) is False


# ── the cross-task index ────────────────────────────────────────────────────
def test_a_collision_is_computed_not_judged(tmp_path):
    index.publish("/ws", task="t-other", cwd="/repo", git_dir="/repo/.git",
                  goal="rewriting config", touched=["services/foo/config.yaml"])
    found = index.collisions("/ws", task="t-mine", cwd="/repo", git_dir="/repo/.git",
                             touched=["services/foo/config.yaml"], ttl_s=900)
    assert len(found) == 1
    assert found[0]["case"] == index.SAME_CHECKOUT
    assert "services/foo/config.yaml" in index.describe(found[0])


def test_separate_worktrees_are_reported_as_the_lower_urgency_case(tmp_path):
    index.publish("/ws", task="t-other", cwd="/repo-wt", git_dir="/repo/.git",
                  goal="branch work", touched=["a.go"])
    found = index.collisions("/ws", task="t-mine", cwd="/repo", git_dir="/repo/.git",
                             touched=["a.go"], ttl_s=900)
    assert found[0]["case"] == index.SEPARATE_WORKTREE


def test_a_different_repository_is_not_a_collision():
    index.publish("/ws", task="t-other", cwd="/other", git_dir="/other/.git",
                  goal="x", touched=["a.go"])
    assert index.collisions("/ws", task="t-mine", cwd="/repo", git_dir="/repo/.git",
                            touched=["a.go"], ttl_s=900) == []


def test_index_entries_expire_rather_than_needing_cleanup():
    index.publish("/ws", task="t-old", cwd="/repo", git_dir="/repo/.git", goal="x",
                  touched=["a.go"])
    assert index.live("/ws", ttl_s=0.0) == []
    assert index.compact("/ws", ttl_s=0.0) == 1


# ── ledger garbage collection ───────────────────────────────────────────────
def test_the_sweep_deletes_dormant_ledgers_but_never_an_active_one():
    old = Ledger(task_id="t-old", workspace="/w")
    old.updated_at = time.time() - 30 * 86400
    paths.write_private(paths.ledger_path("t-old"), json.dumps(old.__dict__))
    Ledger(task_id="t-live", workspace="/w").save()

    removed = sweep(ttl_days=7, max_per_workspace=50, max_bytes=10_000_000,
                    active={"t-live"})
    assert removed == ["t-old"]
    assert paths.ledger_path("t-live").exists()


def test_forget_removes_a_whole_workspace():
    Ledger(task_id="t-a", workspace="/w1").save()
    Ledger(task_id="t-b", workspace="/w2").save()
    assert forget("workspace", workspace="/w1") == ["t-a"]
    assert paths.ledger_path("t-b").exists()


# ── configuration ───────────────────────────────────────────────────────────
def test_values_validate_before_they_are_written():
    with pytest.raises(ConfigError):
        coerce("gate.rate_per_hour", "not a number")
    with pytest.raises(ConfigError):
        coerce("gate.rate_per_hour", 9999)
    with pytest.raises(ConfigError):
        coerce("model.provider", "telepathy")
    with pytest.raises(ConfigError):
        coerce("nonsense.knob", 1)
    assert coerce("gate.rate_per_hour", "2") == 2
    assert coerce("finish_gate.enabled", "off") is False


def test_precedence_is_builtin_then_global_then_workspace():
    set_value("gate.rate_per_hour", 3, scope="global")
    set_value("gate.rate_per_hour", 1, scope="workspace", workspace="/w")
    cfg = Config.load("/w")
    assert cfg.get("gate.rate_per_hour") == 1
    assert cfg.source("gate.rate_per_hour") == "workspace"
    assert Config.load(None).get("gate.rate_per_hour") == 3
    assert Config.load(None).source("gate.cooldown_s") == "built-in"


def test_a_compaction_floor_at_the_threshold_is_refused():
    with pytest.raises(ConfigError, match="thrash"):
        set_value("window.compaction_floor", 0.9, scope="global")
    assert Config.load(None).get("window.compaction_floor") == 0.60


def test_detector_overrides_merge_rather_than_replace():
    from second_brain.config import set_detector
    set_detector("git-log", "enabled", "true", scope="global")
    spec = Config.load(None).group("detectors")["git-log"]
    assert spec["enabled"] is True
    assert spec["system"]                             # the built-in prompt survives
    assert spec["tools"]


def test_muting_is_readable_without_a_worker():
    from second_brain.gate import Mute
    paths.write_private(paths.control_path("t-1"),
                        json.dumps({"detectors": {"default": time.time() + 600}}))
    assert "default" in Mute("t-1", "/w").muted("default")
    assert Mute("t-1", "/w").muted("git-log") == ""


def test_the_spool_offset_survives_a_worker_restart():
    """A restarted worker must not re-read the session it already consumed.

    The spool outlives the worker, so an in-memory offset means a crash replays
    everything: duplicate observations in a fresh window, one enormous episode, and
    the whole bill paid again at the moment nothing is cached.
    """
    feed = [Observation(kind=TEXT, body="first thing"), Observation(kind=TEXT, body="second")]
    spool.append("s-restart", feed)
    assert len(spool.SpoolReader("s-restart").read()) == 2

    restarted = spool.SpoolReader("s-restart")          # a new process would do this
    assert restarted.read() == []

    spool.append("s-restart", [Observation(kind=TEXT, body="after the restart")])
    assert [o.body for o in spool.SpoolReader("s-restart").read()] == ["after the restart"]


def test_starting_at_the_end_ignores_a_stored_offset():
    spool.append("s-end", [Observation(kind=TEXT, body="backlog")])
    assert spool.SpoolReader("s-end", start_at_end=True).read() == []


# ── pricing and the observer/primary ratio ──────────────────────────────────
def test_prices_are_per_million_tokens_with_cache_multipliers():
    from second_brain import pricing

    cost = pricing.estimate("claude-haiku-4-5", {
        "input": 1_000_000, "output": 1_000_000,
        "cache_write": 1_000_000, "cache_read": 1_000_000,
    })
    assert cost.known
    assert cost.input == pytest.approx(1.00)          # Haiku 4.5 input rate
    assert cost.output == pytest.approx(5.00)
    assert cost.cache_write == pytest.approx(1.25)    # 1.25x input at the 5m TTL
    assert cost.cache_read == pytest.approx(0.10)     # 0.1x input
    assert cost.total == pytest.approx(7.35)


def test_the_one_hour_cache_ttl_costs_more_to_write():
    from second_brain import pricing
    at_1h = pricing.estimate("claude-haiku-4-5", {"cache_write": 1_000_000}, cache_ttl="1h")
    assert at_1h.cache_write == pytest.approx(2.00)   # 2x input, not 1.25x


def test_an_unknown_model_is_reported_unpriced_rather_than_guessed():
    from second_brain import pricing
    unknown = pricing.estimate("some-local-model", {"input": 1_000_000})
    assert unknown.known is False and unknown.total == 0.0
    assert "not in the rate table" in unknown.render()

    priced = pricing.estimate("some-local-model", {"input": 1_000_000},
                              override_in=0.5, override_out=1.0)
    assert priced.known and priced.input == pytest.approx(0.5)


def test_a_dated_snapshot_prices_as_its_base_model():
    from second_brain import pricing
    assert pricing.rates("claude-haiku-4-5-20251001") == pricing.rates("claude-haiku-4-5")


def test_one_pass_is_not_a_rate():
    """The first pass runs cold and often carries a larger episode.

    Extrapolating an hourly cost from it reports several times the steady state —
    observed live at $1.07/hour from a single backfill pass.
    """
    from second_brain import pricing
    assert pricing.per_hour(0.19, elapsed_s=600, passes=1) is None   # too few passes
    assert pricing.per_hour(0.19, elapsed_s=60, passes=9) is None    # too little time
    assert pricing.per_hour(0.10, elapsed_s=3600, passes=9) == pytest.approx(0.10)


def test_primary_usage_is_summed_from_the_transcript(tmp_path, monkeypatch):
    """The observer/primary ratio is measured, not asserted — this is the measurement."""
    projects = tmp_path / ".claude" / "projects" / "-some-repo"
    projects.mkdir(parents=True)
    path = projects / "s-usage.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for record in [
            {"type": "assistant", "message": {"usage": {
                "input_tokens": 10, "output_tokens": 5,
                "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 100}}},
            {"type": "assistant", "message": {"usage": {
                "input_tokens": 20, "output_tokens": 7, "cache_read_input_tokens": 2000}}},
            {"type": "user", "message": {"content": "no usage on this one"}},
        ]:
            fh.write(json.dumps(record) + "\n")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    totals = transcript.primary_usage("s-usage")
    assert totals == {"input": 30, "output": 12, "cache_read": 3000, "cache_write": 100}
    assert transcript.primary_usage("no-such-session") == {}

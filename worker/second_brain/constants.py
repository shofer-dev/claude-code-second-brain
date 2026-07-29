"""Every tunable number in the Second Brain, in one place.

Design decision #13 is "no magic numbers": every threshold, cap and interval in
DESIGN.md is a *default* that `/second-brain-config` can change live. This module
is that set of defaults plus the metadata the config command needs to validate a
write and to explain what a knob trades — nothing else imports literals.

`DEFAULTS` is the built-in layer (the bottom of the precedence stack
built-in → global → workspace, see `config.py`). `SPEC` describes each knob:
its type, its permitted range, one line of help, and whether the config command
must print a warning when it is changed (the three knobs that can turn a quiet,
cheap advisor into a loud or expensive one).
"""
from __future__ import annotations

from typing import Any

# ── the built-in configuration layer ────────────────────────────────────────
DEFAULTS: dict[str, dict[str, Any]] = {
    # What of the primary's emissions is forwarded, and how hard it is trimmed.
    "projection": {
        "bash_cap": 400,
        "edit_new": 200,
        "edit_old": 100,
        "write_head": 200,
        "agent_prompt": 400,
        "default_cap": 400,
        "heredoc_head": 120,
        "error_head": 400,
        "harvest_max": 10,
        "text_cap": 4000,
        "user_prompt_cap": 4000,
        "subagent_final_cap": 1500,
        "include_sidechain": False,
        "resolve_edit_anchors": True,
    },
    # When a pass runs. Two limits that both bind, plus a bounded salience escape.
    "loop": {
        "min_interval_s": 90,
        "trigger_chars": 6000,
        "max_interval_s": 900,
        "salience_triggers": ["error", "user_prompt", "turn_end"],
        "salience_per_hour": 6,
        "backoff_on_budget": True,
        "poll_interval_s": 2.0,
        "fork_deadline_s": 20,
        "fork_grace_s": 8,
        "max_parallel_forks": 6,
        "max_fork_iterations": 6,
        "demote_stride": 3,
        "demote_after_timeouts": 2,
        "disable_after_timeouts": 4,
        "demote_retry_s": 1800,
        "episode_cap_chars": 60000,
        "status_interval_s": 30,
    },
    # Window B: append-only between compactions, with hysteresis.
    "window": {
        "budget_chars": 400000,
        "compaction_threshold": 0.85,
        "compaction_floor": 0.60,
    },
    # Task ledgers: task-scoped judgment, TTL-swept.
    "ledger": {
        "ttl_days": 7,
        "max_per_workspace": 50,
        "max_bytes": 8_000_000,
        "sweep_interval_s": 3600,
        "max_entries": 60,
    },
    # The live cross-task index — the one thing that crosses tasks.
    "index": {
        "enabled": True,
        "ttl_s": 900,
        "compact_interval_s": 600,
        "max_paths_per_entry": 40,
    },
    # Between "the model produced an advisory" and "the primary sees it".
    "gate": {
        "confidence_floor": 0.6,
        "human_floor": 0.35,
        "rate_per_hour": 4,
        "cooldown_s": 300,
        "body_cap": 700,
        "headline_cap": 160,
        "dedup_threshold": 0.6,
        "advice_ttl_s": 900,
        "queue_timeout_s": 1800,
        "max_mailbox_entries": 8,
    },
    # The one exception to "never interrupt".
    "finish_gate": {
        "enabled": True,
        "min_interval_s": 3600,
        "per_task_cap": 3,
        "confidence_floor": 0.75,
        "background_settle_s": 600,
    },
    # How long an outcome record stays open before it self-closes as no_evidence.
    "adjudication": {
        "window_observations": 60,
        "window_seconds": 1800,
    },
    # Hard ceilings; exhaustion means silence, never "advise anyway, cheaper".
    "budget": {
        "tokens_per_task": 2_000_000,
        "tokens_per_hour": 600_000,
        "max_output_tokens": 1024,
    },
    # External tools. Servers a detector may be granted, by name; nothing is
    # reachable that is not listed here AND granted by a detector.
    "mcp": {
        "servers": {},
    },
    # Provider + model. Zero-config: the Claude Code subscription, if present.
    "model": {
        "provider": "anthropic",
        "name": "claude-haiku-4-5-20251001",
        "base_url": "",
        "cache_ttl": "5m",
        "request_timeout_s": 120,
    },
    # Whether we observe at all, and where.
    "enable": {
        "default": True,
        "workspaces": {},
    },
}

# Detector definitions are a map, not a fixed set of knobs, so they carry their
# own defaults (see `detectors.py`) and merge per-detector rather than per-key.
DETECTOR_KEYS = (
    "enabled", "system", "tools", "schema", "deadline_s",
    "cadence", "confidence_floor", "pilot", "config",
)

# ── knob metadata: type, range, help, and whether a change is warned about ───
_INT = "int"
_FLOAT = "float"
_BOOL = "bool"
_STR = "str"
_LIST = "list"
_DICT = "dict"

SPEC: dict[str, dict[str, Any]] = {
    "projection.bash_cap": {"type": _INT, "min": 40, "max": 20000,
                            "help": "Bash command characters kept after structure-aware elision."},
    "projection.edit_new": {"type": _INT, "min": 0, "max": 8000,
                            "help": "Characters of an Edit's new_string kept."},
    "projection.edit_old": {"type": _INT, "min": 0, "max": 8000,
                            "help": "Characters of an Edit's old_string kept."},
    "projection.write_head": {"type": _INT, "min": 0, "max": 8000,
                              "help": "Characters of a Write payload kept."},
    "projection.agent_prompt": {"type": _INT, "min": 0, "max": 8000,
                                "help": "Characters of a subagent prompt kept."},
    "projection.default_cap": {"type": _INT, "min": 40, "max": 20000,
                               "help": "Cap for tools with no specific rule."},
    "projection.heredoc_head": {"type": _INT, "min": 0, "max": 4000,
                                "help": "Characters kept from the head of an elided heredoc body."},
    "projection.error_head": {"type": _INT, "min": 0, "max": 8000,
                              "help": "Characters kept from a failing tool result."},
    "projection.harvest_max": {"type": _INT, "min": 0, "max": 100,
                               "help": "Locators harvested out of each elided span."},
    "projection.text_cap": {"type": _INT, "min": 200, "max": 100000,
                            "help": "Cap on one assistant narration block."},
    "projection.user_prompt_cap": {"type": _INT, "min": 200, "max": 100000,
                                   "help": "Cap on one user prompt."},
    "projection.subagent_final_cap": {"type": _INT, "min": 0, "max": 20000,
                                      "help": "Cap on a subagent's final message."},
    "projection.include_sidechain": {"type": _BOOL,
                                     "help": "Observe spawned agents' whole conversations, not just their conclusion."},
    "projection.resolve_edit_anchors": {"type": _BOOL,
                                        "help": "Resolve Edit line anchors by locating old_string on disk."},

    "loop.min_interval_s": {"type": _INT, "min": 5, "max": 7200, "warn": True,
                            "help": "The throttle: no pass may start within this of the previous pass's start."},
    "loop.trigger_chars": {"type": _INT, "min": 200, "max": 2_000_000,
                           "help": "Projected characters accumulated before a pass is due."},
    "loop.max_interval_s": {"type": _INT, "min": 30, "max": 86400,
                            "help": "Liveness floor: a pass at least this often while input is pending."},
    "loop.salience_triggers": {"type": _LIST,
                               "help": "Which events may fire a pass early."},
    "loop.salience_per_hour": {"type": _INT, "min": 0, "max": 240,
                               "help": "Hourly bucket for early (salient) passes."},
    "loop.backoff_on_budget": {"type": _BOOL,
                               "help": "Stretch min_interval_s as the hourly budget depletes."},
    "loop.poll_interval_s": {"type": _FLOAT, "min": 0.1, "max": 60.0,
                             "help": "How often the worker checks the spool for new observations."},
    "loop.fork_deadline_s": {"type": _INT, "min": 2, "max": 600,
                             "help": "Soft deadline told to each fork."},
    "loop.fork_grace_s": {"type": _INT, "min": 1, "max": 300,
                          "help": "Extra time before a fork is hard-cancelled."},
    "loop.max_parallel_forks": {"type": _INT, "min": 1, "max": 32,
                                "help": "Fan-out width."},
    "loop.max_fork_iterations": {"type": _INT, "min": 1, "max": 40,
                                 "help": "Model calls one fork may make before it is stopped."},
    "loop.demote_stride": {"type": _INT, "min": 2, "max": 20,
                           "help": "A demoted detector runs every k-th pass."},
    "loop.demote_after_timeouts": {"type": _INT, "min": 1, "max": 20,
                                   "help": "Consecutive timeouts before a detector is rate-limited."},
    "loop.disable_after_timeouts": {"type": _INT, "min": 2, "max": 40,
                                    "help": "Consecutive timeouts before a detector is disabled for the task."},
    "loop.demote_retry_s": {"type": _INT, "min": 60, "max": 86400,
                            "help": "How long before a disabled detector is retried once."},
    "loop.episode_cap_chars": {"type": _INT, "min": 1000, "max": 5_000_000,
                               "help": "Characters one episode may carry before it coalesces."},
    "loop.status_interval_s": {"type": _INT, "min": 5, "max": 3600,
                               "help": "How often the status file is refreshed while idle."},

    "window.budget_chars": {"type": _INT, "min": 20000, "max": 20_000_000,
                            "help": "Assumed usable window size, in characters."},
    "window.compaction_threshold": {"type": _FLOAT, "min": 0.2, "max": 0.99,
                                    "help": "Fill ratio at which compaction triggers."},
    "window.compaction_floor": {"type": _FLOAT, "min": 0.05, "max": 0.95,
                                "help": "Fill ratio compaction compacts down to (must stay below the threshold)."},

    "ledger.ttl_days": {"type": _INT, "min": 1, "max": 365,
                        "help": "Days a dormant task ledger survives before the sweep deletes it."},
    "ledger.max_per_workspace": {"type": _INT, "min": 1, "max": 10000,
                                 "help": "Ledgers retained per workspace, least-recently-touched evicted."},
    "ledger.max_bytes": {"type": _INT, "min": 100_000, "max": 10_000_000_000,
                         "help": "Total ledger bytes retained."},
    "ledger.sweep_interval_s": {"type": _INT, "min": 60, "max": 86400,
                                "help": "How often a running worker re-sweeps expired ledgers."},
    "ledger.max_entries": {"type": _INT, "min": 5, "max": 1000,
                           "help": "Distilled entries a ledger holds before the oldest are merged out."},

    "index.enabled": {"type": _BOOL, "help": "Publish this task's touched paths to the cross-task index."},
    "index.ttl_s": {"type": _INT, "min": 30, "max": 86400,
                    "help": "How long an index entry stays visible to other tasks."},
    "index.compact_interval_s": {"type": _INT, "min": 30, "max": 86400,
                                 "help": "How often the index file is rewritten without expired entries."},
    "index.max_paths_per_entry": {"type": _INT, "min": 1, "max": 1000,
                                  "help": "Paths carried in one index entry."},

    "gate.confidence_floor": {"type": _FLOAT, "min": 0.0, "max": 1.0,
                              "help": "Below this an advisory reaches the user only, never the agent."},
    "gate.human_floor": {"type": _FLOAT, "min": 0.0, "max": 1.0,
                         "help": "Below this an advisory is dropped entirely."},
    "gate.rate_per_hour": {"type": _INT, "min": 0, "max": 60, "warn": True,
                           "help": "Advisories delivered to the agent per hour — the primary's attention budget."},
    "gate.cooldown_s": {"type": _INT, "min": 0, "max": 86400,
                        "help": "Minimum gap between two delivered advisories."},
    "gate.body_cap": {"type": _INT, "min": 80, "max": 8000,
                      "help": "Characters of advisory body delivered."},
    "gate.headline_cap": {"type": _INT, "min": 20, "max": 1000,
                          "help": "Characters of advisory headline delivered."},
    "gate.dedup_threshold": {"type": _FLOAT, "min": 0.0, "max": 1.0,
                             "help": "Similarity above which a new advisory counts as a duplicate."},
    "gate.advice_ttl_s": {"type": _INT, "min": 30, "max": 86400,
                          "help": "Validity clock: is this still true?"},
    "gate.queue_timeout_s": {"type": _INT, "min": 30, "max": 86400,
                             "help": "Queue clock: is anyone coming to drain it?"},
    "gate.max_mailbox_entries": {"type": _INT, "min": 1, "max": 100,
                                 "help": "Advisories the mailbox holds before the oldest are dropped."},

    "finish_gate.enabled": {"type": _BOOL, "help": "Allow continuing a finished turn with evidenced unfinished work."},
    "finish_gate.min_interval_s": {"type": _INT, "min": 60, "max": 86400, "warn": True,
                                   "help": "Minimum gap between finish-gate interventions, per task."},
    "finish_gate.per_task_cap": {"type": _INT, "min": 0, "max": 100,
                                 "help": "Hard cap on finish-gate interventions for one task."},
    "finish_gate.confidence_floor": {"type": _FLOAT, "min": 0.0, "max": 1.0,
                                     "help": "Confidence an advisory needs to continue a finished turn."},
    "finish_gate.background_settle_s": {"type": _INT, "min": 30, "max": 86400,
                                        "help": "How long a background launch keeps the finish gate silent when nothing reports it finished."},

    "adjudication.window_observations": {"type": _INT, "min": 1, "max": 10000,
                                         "help": "Observations after delivery before an outcome self-closes."},
    "adjudication.window_seconds": {"type": _INT, "min": 30, "max": 86400,
                                    "help": "Seconds after delivery before an outcome self-closes."},

    "budget.tokens_per_task": {"type": _INT, "min": 1000, "max": 1_000_000_000,
                               "help": "Token ceiling for one task; exhaustion means silence."},
    "budget.tokens_per_hour": {"type": _INT, "min": 1000, "max": 1_000_000_000,
                               "help": "Token ceiling per rolling hour."},
    "budget.max_output_tokens": {"type": _INT, "min": 64, "max": 32000,
                                 "help": "Output cap per fork request."},

    "mcp.servers": {"type": _DICT,
                    "help": "MCP servers a detector may be granted, by name: "
                            "{\"code-search\": {\"command\": \"…\", \"args\": [...]}} or "
                            "{\"x\": {\"url\": \"https://…\"}}."},

    "model.provider": {"type": _STR, "choices": ["anthropic", "openai"],
                       "help": "Wire protocol: anthropic Messages, or any OpenAI-compatible endpoint."},
    "model.name": {"type": _STR, "help": "Model id the observer runs on."},
    "model.base_url": {"type": _STR, "help": "Override the provider's base URL (gateways, local models)."},
    "model.cache_ttl": {"type": _STR, "choices": ["5m", "1h"],
                        "help": "Prefix cache lifetime; choose together with loop.min_interval_s."},
    "model.request_timeout_s": {"type": _INT, "min": 5, "max": 900,
                                "help": "HTTP timeout for one provider request."},

    "enable.default": {"type": _BOOL, "help": "Observe workspaces that have made no explicit choice."},
    "enable.workspaces": {"type": _DICT, "help": "Per-workspace opt-in/opt-out, keyed by absolute path."},
}

# Knobs whose change gets an explicit warning in the command's output.
WARNED = tuple(k for k, v in SPEC.items() if v.get("warn"))

# ── things that are not user-tunable ────────────────────────────────────────
# Wire-level constants and file-format identifiers: changing these is a code
# change, not a configuration change, so they are deliberately not in SPEC.
SPOOL_SCHEMA = 1
"""Version stamped on every spool record; a reader that sees a newer one stops."""

OAUTH_BETA_HEADER = "oauth-2025-04-20"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
OPENAI_BASE_URL = "https://api.openai.com/v1"
OAUTH_TOKEN_ENDPOINT = "https://console.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_REFRESH_MARGIN_MS = 5 * 60 * 1000

FEEDBACK_TOOL = "second_brain_detector_feedback"

CHARS_PER_TOKEN = 4.0
"""Soft heuristic for window accounting; real usage is reconciled from the
provider's response and this is never a correctness input."""

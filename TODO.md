# TODO — what is knowingly not done

Gaps, accepted trade-offs and deferred work. Nothing in `README.md` or `DESIGN.md`
may imply a control or a feature that only this file knows is missing.

## Unverified seams (Phase 0 — do this before trusting anything below it)

The monitor is load-bearing twice over: it **hosts** the worker and it **delivers**.
Its *manifest* schema is now verified against the installed CLI; its runtime
behaviour is not, and the design says nothing below these is worth building on
unverified seams.

- [x] **The monitor manifest schema.** Read out of the installed CLI (v2.1.220):
      `monitors/monitors.json` is parsed as a **bare array** of `strictObject`
      entries — `name`, `command`, `description` required, optional `when`
      (`"always"` | `"on-skill-invoke:<skill>"`), names unique within the plugin.
      This caught a real bug: the file was written as `{"monitors": [...]}`, which
      fails the whole plugin's monitor load — and since the monitor hosts the
      worker, it would have failed silently and completely. Locked in by a test.
- [ ] A **plugin-declared** monitor receives `CLAUDE_CODE_SESSION_ID` in its
      environment. Verified for the *Monitor tool* on v2.1.220 by probing a live
      process, and plugin monitors are armed as Monitor tasks by the same
      machinery — but the plugin-declared case has not itself been run
      (a monitor whose command is `env`).
- [ ] A plugin monitor survives for the whole session and can host a long-running
      asyncio process. **If it turns out to be short-lived, the hosting decision
      reverts** to the hook-spawned detached worker — the same code with a worse
      lifetime, already implemented in `spawn.py`.
- [ ] A monitor stdout line actually wakes a *stopped* session. The finish gate's
      deferred half depends on this and on nothing else.
- [ ] `SubagentStop` carries `last_assistant_message` on the installed CLI.
- [ ] `additionalContext` on `PostToolUse` reaches the model without blocking.

Until these are done, treat the hook drain as the only proven channel.

## Not implemented

- **Resumed-task digest on `SessionStart`.** A resumed task's ledger is loaded by
  the worker but never surfaced to the primary (Phase 5 in the design).
- **Staleness check on resuming a long-dormant task.** Open question #9: a task
  resumed after days resumes against a repo that moved underneath it, and the
  ledger's cited locators are not re-validated.

## Accepted trade-offs

- **A slash command always costs a model turn.** Claude Code commands are prompts:
  the `!` block runs, its output is injected, and the model is invoked to relay it.
  The frontmatter schema has no opt-out (verified against the installed CLI —
  `disable-model-invocation` is the inverse, stopping the *model* from invoking the
  command). So `/second-brain-stats` spins the primary loop, which sits awkwardly
  beside the design's claim that the human surfaces cost nothing. The statusline and
  `!` bash mode are the model-free paths, and both are documented in the README.

- **The worker holds its code until the session restarts.** It is a long-lived
  process started at session start, so editing anything under `worker/` has no
  effect on the running one — `/reload-plugins` re-arms commands and hooks but does
  not restart the monitor's process. Symptom seen live: `/second-brain-run` appeared
  to work while the request file was never read, because feeding the spool tripped
  the ordinary volume trigger instead. The command now says so when it detects an
  older worker, and a request expires after five minutes so a stale one cannot fire
  a pass later. Restart the session after changing worker code.

- **The `mcp` SDK is an optional dependency.** `mcpclient.py` is a thin adapter
  over the official SDK — one session per server, shared across forks — but the SDK
  is not vendored, so on a machine without it every MCP-granted tool is absent. That
  is reported (a warning, a status field, `/second-brain-stats`) rather than
  silently reducing a detector's reach, which is the failure mode that matters: a
  detector answering confidently *without* the tool it asked for is worse than one
  that does not answer. Untested against a live server.

- **Transport is ours, not the official SDKs.** `DESIGN.md` §What runs the loop
  argues for the provider SDKs as typed HTTP clients. A Claude Code plugin cannot
  assume it may install packages and the worker has to run on a bare `python3`, so
  `provider.py` builds requests against the published wire formats and posts them
  with `httpx` when present and `urllib` on a worker thread otherwise
  (`http.py`). What that costs, concretely: no streaming, and retries/backoff are
  ours (two retries on 429/5xx). Revisit if the plugin ever ships a vendored
  dependency set.
- **The pilot's cache-warm signal is completion, not first token.** Without
  streaming there is no earlier signal, so the fan-out waits for the pilot fork to
  finish rather than for its first streamed token. Costs latency on every pass,
  never correctness.
- **The window is not reconstructed from the transcript after a crash.** The design
  notes the projection is deterministic and therefore replayable; the worker
  actually restarts from the ledger plus new observations. A restart costs the warm
  prefix *and* the uncompacted tail, not just the cache.
- **Task splitting is a keyword heuristic, not a model judgment.** `task.py` uses
  prefix/marker matching rather than asking the model whether a prompt is a new
  goal. Deliberately wrong in the cheap direction (an extra split costs a cold
  start); open question #3 asks whether it misfires often enough to matter.
- **`static-analysis` ships with an empty command allowlist.** It cannot infer a
  project's build command, and guessing one would be the one detector able to
  execute guessing wrong. The workspace supplies it.
- **Uptake is self-reported.** Constrained by evidence-or-nothing and a default of
  `no_evidence`, but a model grading its own advice is structurally flattering.
  Open question #2: is it honest enough to *tune the gate on*?
- **Per-detector budgets are enforced per fork, not globally.** Two concurrent
  sessions each enforce their own token ceiling; there is no cross-session budget.
  The fix, if it is ever needed, is a lockfile-guarded counter — not a service.

## Catalogue status

Shipped enabled: `default`, `repeat-failure`, `standard-questions` — the design's
shipping order, stopping where tools begin. Defined and disabled: `git-log`,
`prior-art`, `constraint-drift`, `goal-drift`, `cross-task-collision`,
`static-analysis`. Each is one `/second-brain-config` away, and none has been
calibrated against real sessions yet, which is the point of shipping them off.

## Open questions

Carried from `DESIGN.md` §Open questions, unchanged by the implementation — the
first is still the only one that decides whether this is worth having:

1. Does it actually produce better outcomes? The honest test is a month of long
   tasks and the question "would you turn it off?"
2. How honest is self-adjudication?
3. How often does task-boundary detection misfire?
4. Would a detector ever need enough agentic depth to justify losing the shared
   prefix?
5. Should subagents be watched, or only their conclusions?
6. How loud should collision warnings be?
7. How wide can the fan-out go before provider rate limits bind?
8. Does a cheap model have the judgment for this at all?
9. What happens to a task that spans days?

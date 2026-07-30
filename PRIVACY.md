# Privacy

The plugin's whole function is sending a projection of a working session to a model
provider. This document says exactly what that means: what leaves your machine,
what never does, where state is written, and how to turn it off.

## What leaves your machine

Everything the Second Brain observes is sent to the model provider you configured
(by default, Anthropic). Concretely, per session:

| Sent | Detail |
|---|---|
| **The agent's narration** | Assistant text blocks, whole, capped at `projection.text_cap` characters each |
| **Your prompts** | User prompts, whole, capped at `projection.user_prompt_cap` |
| **Tool *arguments*, trimmed** | The command, the path, and a *head* of any payload — see the table below |
| **The head of failing tool results** | Only where `is_error` is set; capped at `projection.error_head` |
| **A subagent's final message** | Capped at `projection.subagent_final_cap` |
| **File contents a detector chooses to read** | Only when a detector has an explicit `Read`/`Grep`/`Glob` grant and decides the judgment depends on it |
| **Output of allowlisted commands** | Only for detectors granted `exec`, only the exact commands listed, capped at 4 000 characters |

Payload heads, by tool (all configurable):

| Tool | What is sent |
|---|---|
| `Bash` | The command, with heredoc bodies and long quoted literals replaced by a marker, then capped at 400 characters |
| `Edit` | The path, a resolved line anchor, the first 200 characters of the new text and 100 of the old |
| `Write` | The path, the first 200 characters of the content, plus its byte and line count |
| `Agent`/`Task` | The subagent type and the first 400 characters of the prompt |
| `Read`/`Grep`/`Glob` | Whole — they are almost entirely paths and patterns |
| anything else | Locator-shaped arguments whole; the rest serialized and capped at 400 characters |

**Paths and line numbers are never trimmed**, including out of elided spans, where
they are harvested into the marker. If your file paths are themselves sensitive,
this plugin is not for that repository.

## What never leaves your machine

- **Successful tool results.** File contents the agent read, command output, search
  results — dropped at projection, never spooled, never sent. This is the design's
  central trade, and it is also its central privacy property.
- **The bodies of files the agent wrote or edited**, beyond the heads above.
- **The agent's reasoning.** Thinking blocks are not persisted by Claude Code in a
  readable form, so no design can send them and this one does not try.
- **Anything from a workspace that has not opted in.** The feed hook checks
  enrolment *before* it opens a transcript.
- **Anything while a mute holds.** `/second-brain-mute all` stops passes and
  delivery: no model call is made and nothing leaves the machine for the duration.
  Observation itself stays local while muted, so what happened during the mute is
  included in the first pass after an unmute — muting silences the output, it does
  not blind the input. To stop observation entirely, disable the workspace
  (`/second-brain-config set enable.default false`).
- **Ledger content, window contents, or advisory text between tasks.** The one
  thing shared across tasks is a live index of task ids, working directories,
  touched paths and timestamps — never text.

## Credentials

By default the observer reuses your **Claude Code subscription** credential from
`~/.claude/.credentials.json`. Two consequences worth stating plainly:

- It draws on that subscription's rate-limit budget, not on metered API billing.
- Refreshed tokens are written to the plugin's **own** data directory
  (`oauth_state.json`, mode 0600). The plugin never writes back to Claude Code's
  credentials file.

Set `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY` with `model.provider openai`) to use
a separate credential instead; an explicit key always wins.

## Where state is written

Under `${CLAUDE_PLUGIN_DATA}` — directories `0700`, files `0600`, owned by you and
unreachable by another user on a shared machine. Nothing listens on a socket or a
port; there is no remote mode.

| Path | Holds | Lifetime |
|---|---|---|
| `spool/<session>.jsonl` | projected observations awaiting the worker | the session |
| `mailbox/<session>.json` | gated advisories awaiting delivery | until delivered or expired |
| `ledgers/<task>.json` | one task's distilled judgment, advice history and suppression keys | the task + `ledger.ttl_days` (7) |
| `history/<task>.jsonl` | every gate decision, for `/second-brain-why` | with the task |
| `index/<workspace>.jsonl` | live cross-task paths and timestamps | `index.ttl_s` (15 min) |
| `status/<session>.json` | the numbers `/second-brain-stats` reads | overwritten each pass |
| `window/<session>.md` | the digest — the observer's context window, verbatim, for `/second-brain-debug` | overwritten on each window change |
| `offsets/`, `state/`, `control/`, `finish-gate/` | transcript cursors, session→task binding, mutes, finish-gate budget | the session or task |
| `config.json`, `workspaces/<hash>.json` | your configuration | until changed |
| `second-brain.log` | the worker's log | appended |

`/second-brain-forget all` deletes every ledger. Removing the plugin's data
directory removes everything above.

## Turning it off

```
/second-brain-mute all                              # stop observing, now
/second-brain-config set enable.default false       # off everywhere by default
/second-brain-config set enable.workspaces '{"/path/to/repo": false}'
```

Or uninstall the plugin: with the hooks gone nothing is read, and with the monitor
gone no worker runs.

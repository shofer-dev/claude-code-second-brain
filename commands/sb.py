#!/usr/bin/env python3
"""The human surfaces, as file operations against the plugin's data directory.

There is no service to query, so every command here reads or writes a file: the
status the worker writes through on each pass, the advice history, the control
files it re-reads, and the config layers. That keeps all of them working whether or
not a worker is running — and when none is, the output says so with a timestamp
instead of pretending to be live (DESIGN.md §Human surfaces).

Everything printed here is for the human. None of it enters the model's context.

Usage: sb.py <stats|why|config|mute|unmute|forget> [args…]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))

from second_brain import gate, ledger as ledgermod, mailbox, paths, status  # noqa: E402
from second_brain.config import GLOBAL, WORKSPACE, Config, ConfigError, reset, set_detector, set_value  # noqa: E402
from second_brain.constants import SPEC, WARNED  # noqa: E402


def _workspace(argv: list[str]) -> str:
    cwd = argv[0] if argv and Path(argv[0]).is_dir() else "."
    return paths.workspace_key(cwd)


def _current(workspace: str) -> dict[str, object] | None:
    for record in status.read_all():
        if record.get("workspace") == workspace:
            return record
    return None


def _age(ts: float) -> str:
    if not ts:
        return "never"
    delta = int(time.time() - ts)
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    return f"{delta // 3600}h{(delta % 3600) // 60:02d}m ago"


# ── stats ───────────────────────────────────────────────────────────────────
def cmd_stats(argv: list[str]) -> int:
    workspace = _workspace(argv)
    record = _current(workspace)
    print("🧠 Second Brain — status")
    print(f"   workspace: {workspace}")
    if record is None:
        print("   no worker has run for this workspace yet (or the plugin is not enrolled here).")
        print("   Advice is generated only while a session is being observed.")
        return 0

    updated = float(record.get("updated_at", 0))
    live = time.time() - updated < 120
    print(f"   state: {record.get('state')}   ({'live' if live else 'stale, last written ' + _age(updated)})")
    print(f"   task: {record.get('task_id')}   model: {record.get('model')}   "
          f"hosted by: {record.get('hosted_by')}")

    raw = int(record.get("observed_raw_chars", 0) or 0)
    kept = int(record.get("observed_chars", 0) or 0)
    share = (kept / raw * 100) if raw else 0.0
    print(f"\n   observed: {kept:,} of {raw:,} emitted characters ({share:.1f}% forwarded)")
    by_tool = record.get("by_tool") or {}
    if isinstance(by_tool, dict) and by_tool:
        print("   per tool (raw → kept):")
        for tool, pair in sorted(by_tool.items(), key=lambda kv: -kv[1][0])[:8]:
            tool_share = (pair[1] / pair[0] * 100) if pair[0] else 0.0
            print(f"     {tool:<16} {pair[0]:>10,} → {pair[1]:>9,}  ({tool_share:5.1f}%)")

    tokens = record.get("tokens") or {}
    print(f"\n   passes: {record.get('passes', 0)}   last: {_age(float(record.get('last_pass_at', 0)))}"
          f" in {record.get('last_pass_s', 0)}s")
    print(f"   window: {int(record.get('window_chars', 0)):,} chars "
          f"({float(record.get('window_fill', 0)) * 100:.0f}% full, "
          f"{record.get('compactions', 0)} compactions)")
    print(f"   observer tokens: in {tokens.get('input', 0):,} · out {tokens.get('output', 0):,} · "
          f"cache read {tokens.get('cache_read', 0):,} · cache write {tokens.get('cache_write', 0):,}")
    print(f"   budget: {int(record.get('budget_task_used', 0)):,} this task · "
          f"{int(record.get('budget_hour_used', 0)):,} this hour")

    print(f"\n   advisories: {record.get('advisories_generated', 0)} generated · "
          f"{record.get('advisories_delivered', 0)} delivered · "
          f"{record.get('advisories_dropped', 0)} gated away")
    detectors = record.get("detectors") or {}
    if isinstance(detectors, dict) and detectors:
        print("   per detector:")
        print(f"     {'detector':<22}{'runs':>6}{'advised':>9}{'sent':>6}{'uptake':>8}"
              f"{'timeouts':>10}  state")
        for name, d in sorted(detectors.items()):
            uptake = d.get("uptake")
            uptake_text = f"{float(uptake) * 100:.0f}%" if uptake is not None else "—"
            print(f"     {name:<22}{d.get('runs', 0):>6}{d.get('advised', 0):>9}"
                  f"{d.get('delivered', 0):>6}{uptake_text:>8}{d.get('timeouts', 0):>10}  "
                  f"{d.get('state', 'active')}")
    pending = mailbox.peek(str(record.get("session_id", "")))
    if pending:
        print(f"\n   waiting to be delivered: {len(pending)}")
        for advisory in pending:
            print(f"     [{advisory.kind}] {advisory.headline[:80]}")
    if record.get("note"):
        print(f"\n   note: {record['note']}")
    print("\n   Silence is the success metric: a chatty Second Brain is a broken one.")
    return 0


# ── why ─────────────────────────────────────────────────────────────────────
def cmd_why(argv: list[str]) -> int:
    workspace = _workspace(argv)
    limit = 20
    for arg in argv:
        if arg.isdigit():
            limit = int(arg)
    record = _current(workspace)
    if record is None:
        print("🧠 Second Brain — no history for this workspace yet.")
        return 0
    task_id = str(record.get("task_id", ""))
    decisions = gate.read_history(task_id, limit)
    ledger = ledgermod.Ledger.load(task_id, workspace)

    print(f"🧠 Second Brain — gate decisions for task {task_id} (newest first)")
    if not decisions:
        print("   nothing has reached the gate yet.")
    for decision in decisions:
        mark = {"delivered": "→ sent", "human_only": "→ user only", "dropped": "✕ dropped"}.get(
            str(decision.get("outcome")), "?")
        print(f"   {time.strftime('%H:%M:%S', time.localtime(float(decision.get('at', 0))))} "
              f"{mark:<13} [{decision.get('detector')}] {str(decision.get('headline'))[:70]}")
        if decision.get("reason"):
            print(f"                 reason: {decision['reason']}")

    closed = [a for a in ledger.advised if a.get("verdict")]
    if closed:
        print("\n   adjudicated advisories (self-reported by the detector that sent them):")
        for advisory in closed[-limit:]:
            print(f"     [{advisory['detector']}] {advisory['headline'][:60]} → {advisory['verdict']}")
            for evidence in advisory.get("verdict_evidence") or []:
                print(f"        evidence: {evidence[:100]}")
        print("\n   Uptake measures whether the agent ACTED, never whether the advice was right.")
    if ledger.suppressed:
        print("\n   suppressed keys (declined once, never raised again this task):")
        for key, why in ledger.suppressed.items():
            print(f"     {key} ({why})")
    return 0


# ── config ──────────────────────────────────────────────────────────────────
def cmd_config(argv: list[str]) -> int:
    workspace = paths.workspace_key(".")
    scope = WORKSPACE
    if "--global" in argv:
        scope = GLOBAL
        argv = [a for a in argv if a != "--global"]
    action = argv[0] if argv else "show"

    if action in {"show", ""}:
        cfg = Config.load(workspace)
        print(f"🧠 Second Brain — effective configuration ({workspace})")
        print("   layer precedence: built-in → global → workspace\n")
        current_group = ""
        for key in sorted(SPEC):
            group = key.split(".")[0]
            if group != current_group:
                current_group = group
                print(f"   [{group}]")
            value = cfg.get(key)
            source = cfg.source(key)
            flag = "  ⚠ changes how loud or costly this is" if key in WARNED else ""
            print(f"     {key.split('.', 1)[1]:<24} {str(value):<28} ({source}){flag}")
        print("\n   [detectors]")
        for name, spec in sorted(cfg.group("detectors").items()):
            tools = spec.get("tools") or []
            print(f"     {name:<22} {'on ' if spec.get('enabled') else 'off'}  "
                  f"cadence={spec.get('cadence', 'every_pass')}  tools={len(tools)}")
        print("\n   /second-brain-config set <group.knob> <value> [--global]")
        print("   /second-brain-config set detectors.<name>.<field> <value>")
        print("   /second-brain-config reset <group|all> [--global]")
        return 0

    try:
        if action == "set":
            if len(argv) < 3:
                print("usage: /second-brain-config set <group.knob> <value>")
                return 1
            key, raw = argv[1], " ".join(argv[2:])
            if key.startswith("detectors."):
                _, name, field = key.split(".", 2)
                value = set_detector(name, field, raw, scope=scope, workspace=workspace)
                print(f"✅ detectors.{name}.{field} = {value}  ({scope})")
                return 0
            value = set_value(key, raw, scope=scope, workspace=workspace)
            print(f"✅ {key} = {value}  ({scope})")
            if key in WARNED:
                print("⚠  This knob decides how loud or how expensive the Second Brain is. "
                      "Watch `/second-brain-stats` after changing it.")
            print("   Running workers pick this up at their next pass boundary — no restart.")
            return 0
        if action == "reset":
            group = argv[1] if len(argv) > 1 else "all"
            reset(group, scope=scope, workspace=workspace)
            print(f"✅ reset {group} to the layer below ({scope})")
            return 0
    except ConfigError as exc:
        print(f"❌ {exc}")
        return 1
    print(f"unknown action: {action}")
    return 1


# ── mute / unmute ───────────────────────────────────────────────────────────
def _control(task_id: str, workspace: str, scope: str) -> Path:
    return (paths.workspace_control_path(workspace) if scope == "workspace"
            else paths.control_path(task_id))


def cmd_mute(argv: list[str], *, mute: bool = True) -> int:
    workspace = paths.workspace_key(".")
    record = _current(workspace)
    task_id = str((record or {}).get("task_id", "")) or "unknown"
    target = argv[0] if argv else "all"
    duration = 0.0
    for arg in argv[1:]:
        if arg.endswith(("m", "h")):
            try:
                duration = float(arg[:-1]) * (60 if arg.endswith("m") else 3600)
            except ValueError:
                duration = 0.0

    scope = "workspace" if target == "workspace" else "task"
    path = _control(task_id, workspace, scope)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}

    if target in {"all", "workspace"}:
        if mute:
            data["all"] = duration == 0.0
            data["all_until"] = time.time() + duration if duration else 0.0
        else:
            data.pop("all", None)
            data.pop("all_until", None)
    else:
        detectors = data.setdefault("detectors", {})
        if mute:
            detectors[target] = (time.time() + duration) if duration else True
        else:
            detectors.pop(target, None)
    paths.write_private(path, json.dumps(data, indent=2))

    window = f" for {int(duration // 60)} minutes" if duration else ""
    if mute:
        print(f"🔇 Second Brain muted: {target}{window} ({scope} scope).")
        if target in {"all", "workspace"}:
            print("   Muting stops observation, not just advice — nothing is sent to the model "
                  "provider while it holds.")
    else:
        print(f"🔊 Second Brain unmuted: {target} ({scope} scope).")
    return 0


# ── forget ──────────────────────────────────────────────────────────────────
def cmd_forget(argv: list[str]) -> int:
    workspace = paths.workspace_key(".")
    scope = argv[0] if argv else "task"
    record = _current(workspace)
    task_id = str((record or {}).get("task_id", ""))
    removed = ledgermod.forget(scope, task_id=task_id, workspace=workspace)
    print(f"🧠 Second Brain — forgot {len(removed)} ledger(s): {', '.join(removed) or 'none'}")
    print("   A ledger is derived state: a running task rebuilds one from its next observations.")
    return 0


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "stats"
    rest = argv[1:]
    if command == "stats":
        return cmd_stats(rest)
    if command == "why":
        return cmd_why(rest)
    if command == "config":
        return cmd_config(rest)
    if command == "mute":
        return cmd_mute(rest, mute=True)
    if command == "unmute":
        return cmd_mute(rest, mute=False)
    if command == "forget":
        return cmd_forget(rest)
    print(__doc__)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:                                       # noqa: BLE001
        print(f"🧠 Second Brain: {type(exc).__name__}: {exc}")
        sys.exit(1)

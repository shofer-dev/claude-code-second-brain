"""Projection goldens — the observation contract, asserted byte-exact.

The projection is a deterministic, model-free function of the transcript, which is
what makes this layer of the harness possible at all: the same input always
produces the same bytes, so a regression in what the observer is allowed to see is
a failing string comparison rather than a judgment call (DESIGN.md §Testing).

The properties under test are the ones the design commits to in prose:
locators survive every rule, every elision leaves a visible marker, successful tool
results never appear, and failures do.
"""
from __future__ import annotations

import json

import pytest

from second_brain.projection import (
    ERROR,
    TEXT,
    TOOL,
    USER,
    harvest_locators,
    project_record,
    project_records,
    project_tool_use,
    resolve_anchor,
)


def assistant(*blocks, sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain, "cwd": "/repo",
            "message": {"role": "assistant", "content": list(blocks)}}


def tool_use(name, **args):
    return {"type": "tool_use", "id": "tu_1", "name": name, "input": args}


# ── the headline rule: results are dropped, failures are not ────────────────
def test_successful_tool_results_are_never_forwarded(projection_cfg):
    record = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "x" * 50_000},
    ]}}
    assert project_record(record, projection_cfg) == []


def test_failing_tool_results_forward_a_capped_head(projection_cfg):
    record = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "is_error": True,
         "content": "main.go:214: undefined: health.Register\n" + "noise\n" * 500},
    ]}}
    [obs] = project_record(record, projection_cfg)
    assert obs.kind == ERROR
    assert len(obs.body) <= projection_cfg["error_head"] + 200
    assert "main.go:214" in obs.body
    assert "main.go:214" in obs.locators          # the cheapest grounded pointer there is


def test_sidechain_records_are_dropped_by_default(projection_cfg):
    record = assistant({"type": "text", "text": "delegated work"}, sidechain=True)
    assert project_record(record, projection_cfg) == []
    assert project_record(record, {**projection_cfg, "include_sidechain": True})


def test_user_prompts_are_forwarded_whole(projection_cfg):
    record = {"type": "user", "message": {"role": "user", "content": "add health probes"}}
    [obs] = project_record(record, projection_cfg)
    assert obs.kind == USER and obs.body == "add health probes"


# ── locators are never elided ───────────────────────────────────────────────
def test_locators_survive_heredoc_elision(projection_cfg):
    body = "\n".join(f"  path: services/foo/deployment.yaml # line {i}" for i in range(60))
    obs = project_tool_use("Bash", {
        "command": f"kubectl apply -f - <<'YAML'\n{body}\nYAML",
        "description": "Apply the updated deployment",
    }, projection_cfg)
    assert "…[" in obs.body                       # the elision is visible
    assert "paths: services/foo/deployment.yaml" in obs.body
    assert "kubectl apply" in obs.body            # the frame still reads as the command


def test_write_keeps_the_path_and_measures_the_body(projection_cfg):
    content = "package health\n" + "\n".join(f"line {i}" for i in range(400))
    obs = project_tool_use("Write", {"file_path": "services/foo/health.go", "content": content},
                           projection_cfg)
    assert "services/foo/health.go" in obs.body
    assert "package health" in obs.body
    assert f"{len(content)} bytes" in obs.body
    assert len(obs.body) < len(content) / 10      # ~10 % kept, per the measurement


def test_read_is_forwarded_whole_including_its_line_range(projection_cfg):
    obs = project_tool_use("Read", {"file_path": "a/b.go", "offset": 88, "limit": 40},
                           projection_cfg)
    assert "a/b.go" in obs.body and "88" in obs.body and "40" in obs.body


def test_unknown_tools_keep_locator_arguments_whole(projection_cfg):
    obs = project_tool_use("SomeNewTool", {
        "file_path": "deep/nested/path/to/file.py",
        "payload": "x" * 5000,
    }, projection_cfg)
    assert "deep/nested/path/to/file.py" in obs.body
    assert len(obs.body) < 1000


@pytest.mark.parametrize("text,expected", [
    # what a locator is
    ("see internal/auth/agent.go:214 and services/foo/deployment.yaml",
     ["internal/auth/agent.go:214", "services/foo/deployment.yaml"]),
    ("main.go:214: undefined: health.Register", ["main.go:214"]),
    ("cd /srv/git && ls ~/.claude/plugins", ["/srv/git", "~/.claude/plugins"]),
    ("edited ../live-memory/DESIGN.md today", ["../live-memory/DESIGN.md"]),
    ("node:internal/modules/package_json_reader:314",
     ["internal/modules/package_json_reader:314"]),
    # …and what only looks like one. Every case below was harvested as a "path" from
    # a real transcript before the pattern was tightened.
    ("prose about agent/human and advisories/hour and model/provider", []),
    ("Object.getPacka ModuleLoader.getModuleJobForRequire", []),
    ("-.fires.-> hookSpecificOutput.additionalContext", []),
    ("version 1.2.3 at https://x.dev/a.go", []),
    ("run go test ./... in claude-code/second-brain", []),
    ("try /second-brain-stats or /loop — transcripts are full of slash commands", []),
])
def test_only_real_coordinates_are_harvested(text, expected):
    assert harvest_locators(text, 10) == expected


# ── edits carry a resolved anchor ───────────────────────────────────────────
def test_edit_anchor_is_resolved_from_disk(tmp_path, projection_cfg):
    target = tmp_path / "main.go"
    target.write_text("package main\n\nfunc main() {\n\t// TODO: health endpoints\n}\n")
    anchor = resolve_anchor(str(target), "\t// TODO: health endpoints\n", None)
    assert anchor == "@L4-L5"

    obs = project_tool_use("Edit", {
        "file_path": str(target), "old_string": "\t// TODO: health endpoints\n",
        "new_string": "\thealth.Register(r, deps)\n",
    }, projection_cfg, str(tmp_path))
    assert "@L4-L5" in obs.body
    assert "health.Register" in obs.body


def test_missing_anchor_is_not_an_error(projection_cfg):
    obs = project_tool_use("Edit", {"file_path": "/nonexistent/x.go", "old_string": "a",
                                    "new_string": "b"}, projection_cfg, "/tmp")
    assert "/nonexistent/x.go" in obs.body        # the locator still survives


# ── determinism, which is what makes the whole harness possible ─────────────
def test_projection_is_deterministic(projection_cfg):
    record = assistant(
        {"type": "text", "text": "The health probes are 404ing."},
        tool_use("Bash", command="go test ./... 2>&1 | tail -5", description="run tests"),
    )
    first = [o.to_json() for o in project_record(record, projection_cfg)]
    second = [o.to_json() for o in project_record(record, projection_cfg)]
    assert first == second


def test_round_trip_through_the_spool_format(projection_cfg):
    from second_brain.projection import Observation
    record = assistant({"type": "text", "text": "hello"}, tool_use("Read", file_path="a.go"))
    for obs in project_records([record], projection_cfg):
        restored = Observation.from_json(obs.to_json())
        assert restored is not None
        assert (restored.kind, restored.body, restored.tool) == (obs.kind, obs.body, obs.tool)


def test_the_worked_example_from_the_design(projection_cfg):
    """Appendix A, end to end: narration kept, payload elided, locators harvested."""
    record = assistant(
        {"type": "text", "text": "The health probes are 404ing because the deployed tag "
                                 "predates the endpoints."},
        tool_use("Write", file_path="services/foo/internal/health/health.go",
                 content="package health\n\nimport (\n\t\"context\"\n" + "x\n" * 400),
        tool_use("Bash", description="Apply the updated deployment",
                 command="kubectl apply -f - <<'YAML'\n"
                         + "apiVersion: apps/v1\n" * 60 + "# services/foo/deployment.yaml\nYAML"),
    )
    observations = project_record(record, projection_cfg)
    kinds = [o.kind for o in observations]
    assert kinds == [TEXT, TOOL, TOOL]

    emitted = len(json.dumps(record))
    observed = sum(len(o.body) for o in observations)
    assert observed < emitted / 2                 # the projection is the main cost lever
    assert "services/foo/internal/health/health.go" in observations[1].body
    assert "…[" in observations[1].body and "…[" in observations[2].body


def test_background_launches_are_flagged_for_the_finish_gate(projection_cfg):
    obs = project_tool_use("Bash", {"command": "make watch", "run_in_background": True},
                           projection_cfg)
    assert obs.meta.get("background") is True
    assert "(background)" in obs.body

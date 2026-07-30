"""Layered, validated, live-reloadable configuration.

Precedence is **built-in defaults → global config → workspace config**, and every
effective value remembers which layer it came from, because a knob you cannot
trace is a knob you cannot trust (DESIGN.md §Configuration). `/second-brain-config`
writes through this module so a rejected value changes nothing and says why, and
running workers pick changes up at their next pass boundary by calling
`Config.load()` again — there is no reload signal and no restart.

Detectors are configured as a map rather than a fixed set of knobs, so they merge
per detector: a workspace that overrides `standard-questions.deadline_s` keeps the
built-in system prompt, tool set and cadence.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .constants import DEFAULTS, SPEC

BUILTIN, GLOBAL, WORKSPACE = "built-in", "global", "workspace"


class ConfigError(ValueError):
    """A rejected `/second-brain-config set` — the message is shown to the user."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge `overlay` into a copy of `base`, one level into each group."""
    out = copy.deepcopy(base)
    for group, values in overlay.items():
        if isinstance(values, dict) and isinstance(out.get(group), dict):
            merged = dict(out[group])
            for key, value in values.items():
                if group == "detectors" and isinstance(value, dict) and isinstance(merged.get(key), dict):
                    merged[key] = {**merged[key], **value}
                else:
                    merged[key] = value
            out[group] = merged
        else:
            out[group] = copy.deepcopy(values)
    return out


@dataclass
class Config:
    """An effective configuration, plus where each value came from."""

    values: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    workspace: str | None = None

    # ── loading ─────────────────────────────────────────────────────────────
    @classmethod
    def load(cls, workspace: str | None = None) -> Config:
        from .detectors import load_catalogue  # local: detectors would import back

        # Two-phase: the catalogue file is itself a config value, so the raw
        # layers are read first to find it, and only then is the built-in layer
        # constructed around whichever catalogue is in force (workspace wins).
        global_layer = _read_json(paths.config_path())
        workspace_layer = _read_json(paths.workspace_config_path(workspace)) if workspace else {}
        catalogue_file = ""
        for layer in (global_layer, workspace_layer):
            section = layer.get("catalogue")
            value = section.get("file") if isinstance(section, dict) else None
            if isinstance(value, str) and value.strip():
                catalogue_file = value.strip()

        builtin = copy.deepcopy(DEFAULTS)
        builtin["detectors"] = load_catalogue(catalogue_file or None)

        layers: list[tuple[str, dict[str, Any]]] = [(BUILTIN, builtin), (GLOBAL, global_layer)]
        if workspace:
            layers.append((WORKSPACE, workspace_layer))

        values: dict[str, Any] = {}
        sources: dict[str, str] = {}
        for name, layer in layers:
            values = _deep_merge(values, layer)
            for group, group_values in layer.items():
                if isinstance(group_values, dict):
                    for key in group_values:
                        sources[f"{group}.{key}"] = name
                else:
                    sources[group] = name
        return cls(values=values, sources=sources, workspace=workspace)

    # ── reading ─────────────────────────────────────────────────────────────
    def get(self, dotted: str, default: Any = None) -> Any:
        group, _, key = dotted.partition(".")
        section = self.values.get(group)
        if not isinstance(section, dict):
            return default
        return section.get(key, default)

    def group(self, name: str) -> dict[str, Any]:
        section = self.values.get(name)
        return dict(section) if isinstance(section, dict) else {}

    def source(self, dotted: str) -> str:
        return self.sources.get(dotted, BUILTIN)

    def flat(self) -> dict[str, Any]:
        """Every scalar knob as `group.key` → value, excluding the detector map."""
        out: dict[str, Any] = {}
        for group, values in self.values.items():
            if group == "detectors":
                continue
            if isinstance(values, dict):
                for key, value in values.items():
                    out[f"{group}.{key}"] = value
        return out

    # ── enablement (DESIGN.md §Scope and consent) ───────────────────────────
    def observing(self, workspace: str) -> bool:
        """Whether this workspace is enrolled. Checked before a transcript is read."""
        per_workspace = self.get("enable.workspaces") or {}
        if isinstance(per_workspace, dict) and workspace in per_workspace:
            return bool(per_workspace[workspace])
        return bool(self.get("enable.default", True))


# ── writing (the /second-brain-config surface) ──────────────────────────────
def coerce(dotted: str, raw: Any) -> Any:
    """Validate and convert one knob's value, or raise ConfigError explaining why.

    Accepts both already-typed values (a worker writing through) and the strings a
    slash command hands over.
    """
    spec = SPEC.get(dotted)
    if spec is None:
        known = ", ".join(sorted(SPEC)[:6])
        raise ConfigError(f"unknown setting '{dotted}' (try `/second-brain-config` to list them; e.g. {known})")

    kind = spec["type"]
    try:
        if kind == "int":
            value: Any = int(str(raw).strip())
        elif kind == "float":
            value = float(str(raw).strip())
        elif kind == "bool":
            text = str(raw).strip().lower()
            if text not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                raise ValueError
            value = text in {"true", "1", "yes", "on"}
        elif kind in {"list", "dict"}:
            value = raw if isinstance(raw, (list, dict)) else json.loads(str(raw))
            if kind == "list" and not isinstance(value, list):
                raise ValueError
            if kind == "dict" and not isinstance(value, dict):
                raise ValueError
        else:
            value = str(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{dotted}: expected {kind}, got {raw!r}") from exc

    if "choices" in spec and value not in spec["choices"]:
        raise ConfigError(f"{dotted}: must be one of {', '.join(map(str, spec['choices']))}")
    if "min" in spec and value < spec["min"]:
        raise ConfigError(f"{dotted}: {value} is below the minimum {spec['min']}")
    if "max" in spec and value > spec["max"]:
        raise ConfigError(f"{dotted}: {value} is above the maximum {spec['max']}")
    return value


def _target(scope: str, workspace: str | None) -> Path:
    if scope == WORKSPACE:
        if not workspace:
            raise ConfigError("no workspace in context — use `--global`")
        return paths.workspace_config_path(workspace)
    return paths.config_path()


def _validate_catalogue_file(path_text: str) -> None:
    """Refuse to store a catalogue path that cannot serve as one RIGHT NOW.

    Load-time already falls back to the bundle on failure, but a silently
    ignored setting is worse than a rejected one: the person believes their
    detectors are in force while the bundled ones run.
    """
    target = Path(path_text).expanduser()
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"catalogue.file: cannot read {target}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"catalogue.file: {target} is not valid JSON: {exc}") from exc
    if (not isinstance(loaded, dict) or not loaded
            or not all(isinstance(v, dict) for v in loaded.values())):
        raise ConfigError(
            "catalogue.file: expected a JSON object mapping detector names to definitions — "
            "copy the bundled worker/second_brain/detectors.json as a starting point")


def set_value(dotted: str, raw: Any, *, scope: str = WORKSPACE, workspace: str | None = None) -> Any:
    """Validate and persist one knob. Returns the stored value."""
    value = coerce(dotted, raw)
    if dotted == "catalogue.file" and str(value).strip():
        _validate_catalogue_file(str(value))
    group, _, key = dotted.partition(".")
    path = _target(scope, workspace)
    data = _read_json(path)
    data.setdefault(group, {})[key] = value
    paths.write_private(path, json.dumps(data, indent=2, sort_keys=True))
    _cross_check(dotted, value, scope, workspace)
    return value


def set_detector(name: str, key: str, raw: Any, *, scope: str = WORKSPACE,
                 workspace: str | None = None) -> Any:
    """Persist one field of one detector (`enabled`, `deadline_s`, `tools`, …)."""
    from .constants import DETECTOR_KEYS

    if key not in DETECTOR_KEYS:
        raise ConfigError(f"unknown detector field '{key}' (one of: {', '.join(DETECTOR_KEYS)})")
    value: Any = raw
    if key == "enabled":
        value = coerce("index.enabled", raw)  # same bool coercion, different home
    elif key == "deadline_s":
        value = coerce("loop.fork_deadline_s", raw)
    elif key == "confidence_floor":
        value = coerce("gate.confidence_floor", raw)
    elif key in {"tools", "config"} and isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"detectors.{name}.{key}: expected JSON, got {raw!r}") from exc

    path = _target(scope, workspace)
    data = _read_json(path)
    detectors = data.setdefault("detectors", {})
    detectors.setdefault(name, {})[key] = value
    paths.write_private(path, json.dumps(data, indent=2, sort_keys=True))
    return value


def reset(group: str, *, scope: str = WORKSPACE, workspace: str | None = None) -> None:
    """Drop a group's overrides at one layer — or every group, for `all`."""
    path = _target(scope, workspace)
    data = _read_json(path)
    if group == "all":
        data = {}
    else:
        data.pop(group, None)
    paths.write_private(path, json.dumps(data, indent=2, sort_keys=True))


def _cross_check(dotted: str, value: Any, scope: str, workspace: str | None) -> None:
    """Reject combinations that are individually valid and jointly broken.

    The compaction floor sitting at or above the threshold is the one that
    matters: it makes every observation trigger a compaction, which rebuilds the
    prefix on every pass and destroys exactly the caching the design rests on.
    """
    if not dotted.startswith("window."):
        return
    cfg = Config.load(workspace)
    threshold = float(cfg.get("window.compaction_threshold", 0.85))
    floor = float(cfg.get("window.compaction_floor", 0.60))
    if floor >= threshold:
        # Undo: leave the stored config as it was rather than half-applied.
        path = _target(scope, workspace)
        data = _read_json(path)
        group, _, key = dotted.partition(".")
        if group in data:
            data[group].pop(key, None)
            if not data[group]:
                data.pop(group)
        paths.write_private(path, json.dumps(data, indent=2, sort_keys=True))
        raise ConfigError(
            "window.compaction_floor must stay below window.compaction_threshold "
            f"(floor {floor} ≥ threshold {threshold}) — equal values re-compact on every "
            "observation and thrash the prefix cache. Change refused."
        )

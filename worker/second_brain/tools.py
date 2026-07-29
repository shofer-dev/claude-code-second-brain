"""What a detector fork may reach, and the jail it reaches through.

Three grant kinds, all per detector, all explicit, and all defaulting to **none**
(DESIGN.md §A detector definition):

| Grant | Example | Provided by |
|---|---|---|
| built-ins | `Read`, `Grep`, `Glob` | this module: read-only and path-jailed |
| commands  | `{"exec": ["git log -20"]}` | this module: exact strings, time-boxed |
| MCP       | `{"mcp": "code-search"}` | `mcpclient.py`, if the SDK is installed |

Two enforcement details that matter more than they look. **The allowlist filters
the tool definitions sent *and* is re-checked at dispatch** — a model can name a
tool it was never offered, so `dispatch` refuses anything outside the detector's
list rather than trusting the request it built. And **nothing here can write**: a
watcher must be structurally incapable of changing the tree it watches, which is
why three read-only tools are implemented here rather than a general tool system
being constrained down to safety.

The path jail is the session's working directory. It is enforced after resolving
symlinks, so a link pointing out of the tree is refused rather than followed.
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_TOOL_RESULT_CHARS = 8000
MAX_GREP_MATCHES = 60
MAX_GLOB_MATCHES = 100
EXEC_TIMEOUT_S = 60
EXEC_OUTPUT_CHARS = 4000

BUILTIN_SCHEMAS: dict[str, dict[str, Any]] = {
    "Read": {
        "name": "Read",
        "description": "Read a file from the observed workspace. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute or workspace-relative path."},
                "offset": {"type": "integer", "description": "First line to read (1-based)."},
                "limit": {"type": "integer", "description": "How many lines to read (default 200)."},
            },
            "required": ["file_path"],
        },
    },
    "Grep": {
        "name": "Grep",
        "description": "Search file contents in the observed workspace with a regular expression.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Directory or file to search under."},
                "glob": {"type": "string", "description": "Filter filenames, e.g. '*.go'."},
            },
            "required": ["pattern"],
        },
    },
    "Glob": {
        "name": "Glob",
        "description": "Find files by glob pattern in the observed workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "e.g. '**/*.go'"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
        },
    },
}

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache"}


class ToolError(RuntimeError):
    """A refused or failed tool call. Returned to the fork as an error result."""


@dataclass
class ToolGrant:
    """One detector's resolved reach."""

    builtins: set[str] = field(default_factory=set)
    commands: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    mcp_tools: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.builtins or self.commands or self.mcp_servers or self.mcp_tools)


def parse_grants(spec: list[Any] | None) -> ToolGrant:
    """Turn a detector's `tools:` list into a grant. Unknown entries are ignored."""
    grant = ToolGrant()
    for entry in spec or []:
        if isinstance(entry, str):
            if entry in BUILTIN_SCHEMAS:
                grant.builtins.add(entry)
            elif entry.startswith("mcp__"):
                grant.mcp_tools.append(entry)
        elif isinstance(entry, dict):
            for command in entry.get("exec", []) or []:
                if isinstance(command, str) and command.strip():
                    grant.commands.append(command.strip())
            server = entry.get("mcp")
            if isinstance(server, str):
                grant.mcp_servers.append(server)
            elif isinstance(server, list):
                grant.mcp_servers.extend(str(s) for s in server)
    return grant


class Toolbox:
    """Resolves grants to tool definitions and dispatches calls, inside the jail."""

    def __init__(self, root: str | Path, mcp: Any = None) -> None:
        self.root = Path(root).resolve()
        self.mcp = mcp

    # ── definitions ─────────────────────────────────────────────────────────
    def definitions(self, grant: ToolGrant) -> list[dict[str, Any]]:
        """The tool list sent with a fork's request — grants only, nothing ambient."""
        out = [BUILTIN_SCHEMAS[name] for name in sorted(grant.builtins)]
        if grant.commands:
            out.append({
                "name": "Run",
                "description": ("Run one of the commands this detector is allowed to run, in the "
                                "workspace root. The command must be given exactly as listed."),
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string", "enum": list(grant.commands)}},
                    "required": ["command"],
                },
            })
        if self.mcp is not None:
            out.extend(self.mcp.definitions(grant))
        return out

    # ── dispatch ────────────────────────────────────────────────────────────
    async def dispatch(self, name: str, args: dict[str, Any], grant: ToolGrant) -> str:
        """Execute one tool call, re-checking the grant. Never raises to the caller."""
        try:
            if name in BUILTIN_SCHEMAS:
                if name not in grant.builtins:
                    raise ToolError(f"{name} is not available to this detector")
                if name == "Read":
                    return self._read(args)
                if name == "Grep":
                    return self._grep(args)
                return self._glob(args)
            if name == "Run":
                return await self._run(args, grant)
            if self.mcp is not None and name.startswith("mcp__"):
                return await self.mcp.call(name, args, grant)
            raise ToolError(f"unknown tool {name}")
        except ToolError as exc:
            return f"error: {exc}"
        except Exception as exc:                                   # noqa: BLE001
            return f"error: {type(exc).__name__}: {exc}"

    # ── the jail ────────────────────────────────────────────────────────────
    def _resolve(self, raw: str | None, *, must_exist: bool = True) -> Path:
        candidate = Path(raw) if raw else self.root
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise ToolError(f"cannot resolve {raw!r}") from exc
        if resolved != self.root and self.root not in resolved.parents:
            raise ToolError(f"path outside the observed workspace: {raw}")
        if must_exist and not resolved.exists():
            raise ToolError(f"no such path: {raw}")
        return resolved

    # ── built-ins ───────────────────────────────────────────────────────────
    def _read(self, args: dict[str, Any]) -> str:
        path = self._resolve(str(args.get("file_path", "")))
        if not path.is_file():
            raise ToolError(f"not a file: {args.get('file_path')}")
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, min(2000, int(args.get("limit") or 200)))
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                lines = []
                for number, line in enumerate(fh, start=1):
                    if number < offset:
                        continue
                    if number >= offset + limit:
                        break
                    lines.append(f"{number}\t{line.rstrip()}")
        except OSError as exc:
            raise ToolError(str(exc)) from exc
        return "\n".join(lines)[:MAX_TOOL_RESULT_CHARS] or "(empty)"

    def _walk(self, base: Path, glob: str | None) -> list[Path]:
        out: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for filename in filenames:
                if glob and not fnmatch.fnmatch(filename, glob):
                    continue
                out.append(Path(dirpath) / filename)
                if len(out) > 5000:
                    return out
        return out

    def _grep(self, args: dict[str, Any]) -> str:
        try:
            pattern = re.compile(str(args.get("pattern", "")))
        except re.error as exc:
            raise ToolError(f"bad pattern: {exc}") from exc
        base = self._resolve(args.get("path"))
        targets = [base] if base.is_file() else self._walk(base, args.get("glob"))
        hits: list[str] = []
        for target in targets:
            try:
                with target.open("r", encoding="utf-8", errors="replace") as fh:
                    for number, line in enumerate(fh, start=1):
                        if pattern.search(line):
                            rel = target.relative_to(self.root)
                            hits.append(f"{rel}:{number}: {line.strip()[:200]}")
                            if len(hits) >= MAX_GREP_MATCHES:
                                hits.append("… (truncated)")
                                return "\n".join(hits)
            except (OSError, UnicodeDecodeError):
                continue
        return "\n".join(hits) or "no matches"

    def _glob(self, args: dict[str, Any]) -> str:
        base = self._resolve(args.get("path"))
        pattern = str(args.get("pattern", "*"))
        try:
            matches = sorted(base.glob(pattern))[:MAX_GLOB_MATCHES]
        except (OSError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return "\n".join(str(m.relative_to(self.root)) for m in matches) or "no matches"

    # ── allowlisted commands ────────────────────────────────────────────────
    async def _run(self, args: dict[str, Any], grant: ToolGrant) -> str:
        command = str(args.get("command", "")).strip()
        if command not in grant.commands:
            # The re-check that matters: the model may name a command it was never
            # offered, and the tool definition's enum is a hint, not a boundary.
            raise ToolError(f"command not allowed for this detector: {command!r}")
        try:
            process = await asyncio.create_subprocess_exec(
                *shlex.split(command), cwd=str(self.root),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            raise ToolError(f"cannot run: {exc}") from exc
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=EXEC_TIMEOUT_S)
        except asyncio.TimeoutError:
            process.kill()
            raise ToolError(f"timed out after {EXEC_TIMEOUT_S}s: {command}") from None
        output = stdout.decode("utf-8", "replace")
        if len(output) > EXEC_OUTPUT_CHARS:
            output = output[:EXEC_OUTPUT_CHARS] + f"\n… (+{len(output) - EXEC_OUTPUT_CHARS} chars)"
        return f"exit {process.returncode}\n{output}"

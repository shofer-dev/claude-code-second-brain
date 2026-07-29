"""The observation contract, as a pure function.

This module turns Claude Code transcript records into the observations the Second
Brain is allowed to see. It is the whole of DESIGN.md §The observation contract in
code, and it has one property everything else leans on: **it is deterministic and
model-free**. The same transcript always produces the same bytes, so observing
costs zero tokens, a lost window can be rebuilt from disk, and the golden tests
can assert byte-exact output (§Accumulation is free; only judgment costs).

Three rules, in the order they are applied:

1. **Emissions only.** Assistant narration, tool *arguments*, user prompts. Tool
   results are dropped — except a failure's head and a subagent's final message.
2. **Structure-aware elision first, cap second.** A heredoc whose body is replaced
   by a marker still reads as the command it is; the same command hard-truncated
   at 400 characters reads as garbage.
3. **Locators are never elided.** Paths, line numbers, globs and patterns survive
   every rule above, including elision — they are harvested *out* of the spans
   that get cut. They are the observer's index into the repository, and what makes
   its advice checkable.

Every elision leaves a marker naming what was removed, because a truncation the
observer cannot see is a truncation it will reason past.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .constants import SPOOL_SCHEMA

# ── observation record ──────────────────────────────────────────────────────
TEXT, TOOL, USER, ERROR, SUBAGENT, META = "text", "tool", "user", "error", "subagent", "meta"

SALIENT_KINDS = {USER, ERROR}


@dataclass
class Observation:
    """One projected emission. `body` is what the model reads; the rest is bookkeeping."""

    kind: str
    body: str
    ts: float = 0.0
    tool: str = ""
    locators: list[str] = field(default_factory=list)
    raw_chars: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def kept_chars(self) -> int:
        return len(self.body)

    @property
    def salient(self) -> bool:
        return self.kind in SALIENT_KINDS

    def to_json(self) -> str:
        return json.dumps({
            "v": SPOOL_SCHEMA, "kind": self.kind, "ts": round(self.ts, 3), "tool": self.tool,
            "body": self.body, "locators": self.locators, "raw": self.raw_chars,
            "meta": self.meta,
        }, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> Observation | None:
        try:
            d = json.loads(line)
        except ValueError:
            return None
        if not isinstance(d, dict) or d.get("v") != SPOOL_SCHEMA:
            return None
        return cls(
            kind=str(d.get("kind", META)), body=str(d.get("body", "")),
            ts=float(d.get("ts", 0.0)), tool=str(d.get("tool", "")),
            locators=list(d.get("locators") or []), raw_chars=int(d.get("raw", 0)),
            meta=dict(d.get("meta") or {}),
        )

    def render(self) -> str:
        """How this observation appears in Window B."""
        if self.kind == TEXT:
            return f"[text]  {self.body}"
        if self.kind == USER:
            return f"[user]  {self.body}"
        if self.kind == ERROR:
            return f"[error] {self.tool or 'tool'} failed: {self.body}"
        if self.kind == SUBAGENT:
            return f"[agent] {self.tool or 'subagent'} concluded: {self.body}"
        if self.kind == TOOL:
            return f"[tool]  {self.body}"
        return f"[meta]  {self.body}"


# ── locators ────────────────────────────────────────────────────────────────
# A path-shaped token: a directory chain and/or a dotted filename, with an optional
# :line or :line-line suffix.
#
# Deliberately conservative, and tuned against real transcripts rather than guessed:
# a false negative costs one harvested coordinate, while a false positive puts a
# file that does not exist into a marker the observer reads as evidence. Prose with
# slashes (`agent/human`, `advisories/hour`) and dotted identifiers
# (`ModuleLoader.getModuleJobForRequire`) are the two cases that actually occur, so
# the extension is restricted to a short lowercase suffix and a trailing word
# boundary is required — without it the pattern matches a *prefix* of a long method
# name and reports the fragment as a filename.
_LOCATOR = re.compile(
    r"(?<![\w@:/])(?:~|\.{1,2})?(?:/[\w.\-]+)+(?::\d+(?:-\d+)?)?"         # /a/b, ./a/b, ~/a
    r"|(?<![\w@/.])[\w\-]*[A-Za-z0-9][\w\-]*(?:/[\w.\-]+)+(?::\d+(?:-\d+)?)?"   # a/b/c
    r"|(?<![\w@/.])[\w\-]*[A-Za-z0-9][\w\-]*\.[a-z][a-z0-9]{0,4}(?![\w.])(?::\d+(?:-\d+)?)?"
)
_NOT_A_PATH = re.compile(r"^(?:\d+(?:\.\d+)*|https?:.*)$")
_HAS_EXTENSION = re.compile(r"\.[a-z][a-z0-9]{0,4}(?::\d+(?:-\d+)?)?$")
# Two segments, not one: `/srv/git` is a directory worth keeping, `/stats` is a
# slash command. Claude Code transcripts are full of the latter, so a single rooted
# segment with no extension is rejected — at the cost of missing a bare `/etc`.
_ROOTED = re.compile(r"^(?:~|\.{1,2})?/[^/]+/")


def _plausible(token: str) -> bool:
    """Whether a matched token is worth reporting as a coordinate.

    One of three things has to be true: it names a file (an extension), it points at
    a line (`:214`), or it is a rooted path of at least two segments (`/srv/git`,
    `~/.claude/plugins`) — which `cd` targets are, and slash commands are not. In
    every case something has to remain after the root, or `./...` reports as a path.
    """
    if not re.search(r"[A-Za-z0-9]", token.lstrip("./~")):
        return False
    return bool(_HAS_EXTENSION.search(token) or ":" in token or _ROOTED.match(token))

# Argument keys that are locators by nature and are therefore never trimmed.
LOCATOR_KEYS = ("file_path", "notebook_path", "path", "glob", "pattern", "paths",
                "file", "filePath", "offset", "limit")


def harvest_locators(text: str, limit: int, *, cut_at_start: bool = False) -> list[str]:
    """Extract up to `limit` distinct path-shaped tokens, in order of appearance.

    `cut_at_start` is set when the text is an elided span, which by construction
    begins at an arbitrary cut point: a match starting at index 0 is then a
    *fragment* of a token whose head stayed behind (`…deploym|ent.yaml`), and
    reporting it as a path would put a file that does not exist into the marker.
    """
    if limit <= 0 or not text:
        return []
    seen: list[str] = []
    for match in _LOCATOR.finditer(text):
        if cut_at_start and match.start() == 0 and (text[0].isalnum() or text[0] in "._-"):
            continue
        token = match.group(0).rstrip(".,;:)\"'")
        if not token or _NOT_A_PATH.match(token) or len(token) > 200 or not _plausible(token):
            continue
        if token not in seen:
            seen.append(token)
            if len(seen) >= limit:
                break
    return seen


# ── elision ─────────────────────────────────────────────────────────────────
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:4]


def _human_chars(n: int) -> str:
    return f"{n / 1000:.1f}k chars" if n >= 1000 else f"{n} chars"


def marker(removed: str, cfg: dict[str, Any], *, word: str = "") -> str:
    """The visible record of what was cut, with coordinates harvested out of it."""
    if not removed:
        return ""
    lines = removed.count("\n")
    parts = [f"+{lines} lines" if lines else f"+{len(removed)} chars"]
    if lines:
        parts.append(_human_chars(len(removed)))
    parts.append(f"sha {_sha(removed)}")
    body = ", ".join(parts)
    if word:
        body = f"{body} {word}"
    found = harvest_locators(removed, int(cfg.get("harvest_max", 10)), cut_at_start=True)
    if found:
        body = f"{body}; paths: {', '.join(found)}"
    return f"…[{body}]"


def cap(text: str, limit: int, cfg: dict[str, Any]) -> str:
    """Truncate to `limit`, leaving a marker that names what the tail contained."""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + marker(text[limit:], cfg)


# ── Bash: structure-aware elision, then the cap ─────────────────────────────
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w]*)\1")
_QUOTED = re.compile(r"'([^']{200,})'|\"((?:[^\"\\]|\\.){200,})\"", re.DOTALL)


def elide_heredocs(command: str, cfg: dict[str, Any]) -> str:
    """Replace heredoc bodies with a head plus a marker, leaving the frame intact.

    The frame matters: `kubectl apply -f - <<'YAML' … YAML` still reads as a
    kubectl apply against a manifest, which is the intent the observer needs.
    """
    head_len = int(cfg.get("heredoc_head", 120))
    out: list[str] = []
    pos = 0
    for match in _HEREDOC.finditer(command):
        terminator = match.group(2)
        body_start = command.find("\n", match.end())
        if body_start == -1:
            continue
        body_start += 1
        end = re.compile(rf"^\s*{re.escape(terminator)}\s*$", re.MULTILINE).search(command, body_start)
        body_end = end.start() if end else len(command)
        body = command[body_start:body_end]
        if len(body) <= head_len:
            continue
        if body_start < pos:  # nested/overlapping heredoc already handled
            continue
        out.append(command[pos:body_start])
        head = body[:head_len]
        out.append(head + "\n" + marker(body[head_len:], cfg, word="elided") + "\n")
        pos = body_end
    out.append(command[pos:])
    return "".join(out)


def elide_long_literals(command: str, cfg: dict[str, Any]) -> str:
    """Replace long quoted payloads with a head plus a marker."""
    head_len = int(cfg.get("heredoc_head", 120))

    def repl(match: re.Match[str]) -> str:
        quote = "'" if match.group(1) is not None else '"'
        body = match.group(1) if match.group(1) is not None else match.group(2)
        return f"{quote}{body[:head_len]}{marker(body[head_len:], cfg)}{quote}"

    return _QUOTED.sub(repl, command)


def project_bash(args: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, list[str]]:
    command = str(args.get("command", ""))
    description = str(args.get("description", "") or "")
    raw = command
    trimmed = elide_long_literals(elide_heredocs(command, cfg), cfg)
    trimmed = cap(trimmed, int(cfg.get("bash_cap", 400)), cfg)
    head = f'Bash — "{description}"\n' if description else "Bash\n"
    if args.get("run_in_background"):
        head = head.rstrip("\n") + "  (background)\n"
    return head + _indent(trimmed), harvest_locators(raw, int(cfg.get("harvest_max", 10)))


# ── the other tools ─────────────────────────────────────────────────────────
def _indent(text: str, prefix: str = "        ") -> str:
    return "\n".join(prefix + line for line in text.splitlines()) or prefix


def _inline(text: str) -> str:
    """Render a file payload on one line, escaping its structure.

    Payload heads are shown as a quoted, escaped string rather than as indented
    lines. That is not cosmetic: indenting a 200-character head that happens to
    span thirty lines adds 240 characters of prefix, so the *presentation* would
    end up costing more than the content it presents.
    """
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace('"', '\\"')


def _line_span(text: str) -> str:
    lines = text.count("\n") + 1 if text else 0
    return f"{lines} lines" if lines else "empty"


def resolve_anchor(file_path: str, old_string: str, cwd: str | None) -> str:
    """`@L88-L94` for an Edit, located on disk — the arguments carry no line numbers.

    Free (a local read), and it is what turns "edited something in the auth
    package" into a coordinate the observer can `Read`, `git log -L` or grep
    around. Returns an empty string when the file or the anchor is not found;
    a missing anchor is never an error.
    """
    if not file_path or not old_string:
        return ""
    try:
        path = Path(file_path)
        if not path.is_absolute() and cwd:
            path = Path(cwd) / path
        content = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""
    idx = content.find(old_string)
    if idx == -1:
        return ""
    start = content.count("\n", 0, idx) + 1
    end = start + old_string.count("\n")
    return f"@L{start}" if start == end else f"@L{start}-L{end}"


def project_edit(name: str, args: dict[str, Any], cfg: dict[str, Any], cwd: str | None) -> tuple[str, list[str]]:
    path = str(args.get("file_path") or args.get("notebook_path") or "")
    old = str(args.get("old_string", "") or "")
    new = str(args.get("new_string", "") or args.get("new_source", "") or "")
    anchor = ""
    if cfg.get("resolve_edit_anchors", True):
        anchor = resolve_anchor(path, old, cwd)
    header = f"{name} {path}{(' ' + anchor) if anchor else ''}"
    if args.get("replace_all"):
        header += "  (replace_all)"
    body = []
    if new:
        body.append(f'+ "{_inline(cap(new, int(cfg.get("edit_new", 200)), cfg))}"  ({_line_span(new)})')
    if old:
        body.append(f'- "{_inline(cap(old, int(cfg.get("edit_old", 100)), cfg))}"  ({_line_span(old)})')
    locators = [path] if path else []
    locators += harvest_locators(old + "\n" + new, int(cfg.get("harvest_max", 10)))
    return header + "\n" + _indent("\n".join(body)), locators


def project_write(args: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, list[str]]:
    path = str(args.get("file_path", "") or "")
    content = str(args.get("content", "") or "")
    head = _inline(cap(content, int(cfg.get("write_head", 200)), cfg))
    detail = f'content: "{head}"  ({_line_span(content)}, {len(content)} bytes)'
    locators = [path] if path else []
    locators += harvest_locators(content[: 4000], int(cfg.get("harvest_max", 10)))
    return f"Write {path}\n" + _indent(detail), locators


def project_agent(name: str, args: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, list[str]]:
    subagent = str(args.get("subagent_type") or args.get("agent_type") or "general-purpose")
    prompt = str(args.get("prompt", "") or "")
    description = str(args.get("description", "") or "")
    head = f"{name} → {subagent}" + (f' — "{description}"' if description else "")
    body = cap(prompt, int(cfg.get("agent_prompt", 400)), cfg)
    return head + "\n" + _indent(body), harvest_locators(prompt, int(cfg.get("harvest_max", 10)))


def project_verbatim(name: str, args: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, list[str]]:
    """Read/Glob/Grep/MCP: already small, and almost entirely locators."""
    rendered = ", ".join(f"{k}={_short(v)}" for k, v in args.items() if v is not None)
    text = f"{name} {rendered}" if rendered else name
    return text, harvest_locators(text, int(cfg.get("harvest_max", 10)))


def _short(value: Any) -> str:
    if isinstance(value, str):
        return value if len(value) <= 200 else value[:200] + "…"
    return json.dumps(value, ensure_ascii=False)[:200]


VERBATIM_TOOLS = {"Read", "Glob", "Grep", "LS", "TodoWrite", "WebFetch", "WebSearch"}
EDIT_TOOLS = {"Edit", "MultiEdit", "NotebookEdit"}
AGENT_TOOLS = {"Agent", "Task"}


def project_tool_use(name: str, args: dict[str, Any], cfg: dict[str, Any],
                     cwd: str | None = None) -> Observation:
    """Project one `tool_use` block into a single observation."""
    raw = len(json.dumps(args, ensure_ascii=False, default=str))
    if name == "Bash":
        body, locators = project_bash(args, cfg)
    elif name in EDIT_TOOLS:
        body, locators = project_edit(name, args, cfg, cwd)
    elif name == "Write":
        body, locators = project_write(args, cfg)
    elif name in AGENT_TOOLS:
        body, locators = project_agent(name, args, cfg)
    elif name in VERBATIM_TOOLS or name.startswith("mcp__"):
        body, locators = project_verbatim(name, args, cfg)
    else:
        kept = _keep_locator_keys(args, cfg)
        body, locators = kept
    obs = Observation(kind=TOOL, tool=name, body=body, locators=_dedup(locators), raw_chars=raw)
    # Work the primary launched but did not wait for. The finish gate needs this:
    # a turn that ends with a background command still outstanding is not a claim
    # that the job is done (§The finish gate).
    if args.get("run_in_background") or name in {"Monitor", "TaskCreate"}:
        obs.meta["background"] = True
    return obs


def _keep_locator_keys(args: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, list[str]]:
    """Fallback rule: serialize, cap — but keep every locator-shaped argument whole."""
    kept = {k: v for k, v in args.items() if k in LOCATOR_KEYS and v is not None}
    rest = {k: v for k, v in args.items() if k not in kept}
    text = json.dumps(rest, ensure_ascii=False, default=str) if rest else ""
    body = cap(text, int(cfg.get("default_cap", 400)), cfg)
    if kept:
        prefix = ", ".join(f"{k}={_short(v)}" for k, v in kept.items())
        body = f"{prefix} {body}".strip()
    locators = harvest_locators(" ".join(str(v) for v in kept.values()) + " " + text,
                                int(cfg.get("harvest_max", 10)))
    return body, locators


def _dedup(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


# ── transcript records → observations ───────────────────────────────────────
def _content_blocks(message: Any) -> list[dict[str, Any]]:
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = message
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return json.dumps(content, ensure_ascii=False, default=str) if content is not None else ""


def _timestamp(record: dict[str, Any]) -> float:
    raw = record.get("timestamp")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        from datetime import datetime
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def project_record(record: dict[str, Any], cfg: dict[str, Any]) -> list[Observation]:
    """Project one transcript JSONL record. Returns zero or more observations."""
    if not isinstance(record, dict):
        return []
    if record.get("isSidechain") and not cfg.get("include_sidechain", False):
        return []

    kind = record.get("type")
    ts = _timestamp(record)
    cwd = record.get("cwd")
    out: list[Observation] = []

    if kind == "assistant":
        for block in _content_blocks(record.get("message")):
            btype = block.get("type")
            if btype == "text":
                text = str(block.get("text", "")).strip()
                if text:
                    out.append(Observation(kind=TEXT, ts=ts, raw_chars=len(text),
                                           body=cap(text, int(cfg.get("text_cap", 4000)), cfg)))
            elif btype == "tool_use":
                obs = project_tool_use(str(block.get("name", "tool")),
                                       block.get("input") or {}, cfg, cwd)
                obs.ts = ts
                obs.meta["tool_use_id"] = block.get("id", "")
                out.append(obs)
        return out

    if kind == "user":
        blocks = _content_blocks(record.get("message"))
        # A user record is either a real prompt or the harness handing back tool
        # results. Only the failures of the latter are forwarded.
        for block in blocks:
            if block.get("type") == "tool_result":
                if not block.get("is_error"):
                    continue
                text = _result_text(block).strip()
                if not text:
                    continue
                out.append(Observation(
                    kind=ERROR, ts=ts, raw_chars=len(text),
                    body=cap(text, int(cfg.get("error_head", 400)), cfg),
                    locators=harvest_locators(text[:4000], int(cfg.get("harvest_max", 10))),
                    meta={"tool_use_id": block.get("tool_use_id", "")},
                ))
            elif block.get("type") == "text" and not record.get("toolUseResult"):
                text = str(block.get("text", "")).strip()
                if text and not text.startswith("<"):
                    out.append(Observation(kind=USER, ts=ts, raw_chars=len(text),
                                           body=cap(text, int(cfg.get("user_prompt_cap", 4000)), cfg)))
        return out

    return out


def project_records(records: Iterable[dict[str, Any]], cfg: dict[str, Any]) -> list[Observation]:
    out: list[Observation] = []
    for record in records:
        out.extend(project_record(record, cfg))
    return out

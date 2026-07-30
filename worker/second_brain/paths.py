"""Where state lives on disk, and the rules that keep it private.

Everything durable in this plugin is a file under `${CLAUDE_PLUGIN_DATA}` — there
is no daemon, no socket and no listener, so the trust boundary reduces to
filesystem permissions (DESIGN.md §Trust boundary). Hence two invariants enforced
here rather than at each call site: **directories are created 0700 and files
0600**, and nothing outside this module builds a state path by hand.

Workspaces are keyed by the *enclosing git repository root* when there is one, so
a session started in a subdirectory shares a workspace with one started at the
top. Filenames derived from a path are hashed, never embedded, because a path can
contain characters a filename cannot.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def data_dir() -> Path:
    """The plugin's state root, created if missing.

    `CLAUDE_PLUGIN_DATA` is set by Claude Code for hooks, monitors and MCP
    servers; the fallback keeps the worker and the offline test harness working
    when it is not (a bare `python3 run.py`, a replay run).
    """
    env = os.environ.get("CLAUDE_PLUGIN_DATA")
    root = Path(env) if env else Path.home() / ".claude" / "plugins" / "data" / "second-brain"
    return ensure_dir(root)


def ensure_dir(p: Path) -> Path:
    """Create `p` (and parents) private to this user, and return it."""
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def write_private(path: Path, text: str) -> None:
    """Atomically write `text` to `path` with mode 0600.

    Atomic because a reader (a hook, another worker) may open the file at any
    moment and a half-written JSON document is indistinguishable from corruption.
    """
    ensure_dir(path.parent)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def append_private(path: Path, line: str) -> None:
    """Append one newline-terminated line, creating the file 0600 if needed.

    A single `O_APPEND` write below the pipe-buffer limit is atomic between
    processes, which is what lets concurrent hooks and workers share the spool
    and the cross-task index without a lock (DESIGN.md §It is a file, not a
    service).
    """
    ensure_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line.rstrip("\n") + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def slug(value: str, length: int = 16) -> str:
    """A filename-safe, collision-resistant key for an arbitrary string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def safe_name(value: str) -> str:
    """A filename-safe rendering of an id that is already short and opaque."""
    return _SAFE.sub("_", value)[:80] or "unknown"


def workspace_key(cwd: str | Path) -> str:
    """Canonical workspace identity for `cwd`: its git repo root, else itself.

    Cached on disk because this runs in every hook invocation and a `git
    rev-parse` subprocess per tool call is a cost the primary would feel — the one
    thing this plugin is not allowed to do.
    """
    p = Path(cwd).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass

    cache_file = data_dir() / "workspace-keys.json"
    cache: dict[str, str] = {}
    try:
        loaded = json.loads(cache_file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            cache = loaded
    except (OSError, ValueError):
        pass
    hit = cache.get(str(p))
    if isinstance(hit, str) and hit:
        return hit

    key = _git_root(p)
    cache[str(p)] = key
    if len(cache) > 500:  # bounded: this is a cache, not a record
        cache = dict(list(cache.items())[-250:])
    try:
        write_private(cache_file, json.dumps(cache))
    except OSError:
        pass
    return key


def _git_root(p: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(p), capture_output=True, text=True, timeout=3, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return str(p)


# ── the state layout (DESIGN.md §Packaging) ─────────────────────────────────
def spool_path(session_id: str) -> Path:
    return data_dir() / "spool" / f"{safe_name(session_id)}.jsonl"


def mailbox_path(session_id: str) -> Path:
    return data_dir() / "mailbox" / f"{safe_name(session_id)}.json"


def offset_path(session_id: str) -> Path:
    return data_dir() / "offsets" / f"{safe_name(session_id)}.json"


def spool_offset_path(session_id: str) -> Path:
    """How far the worker has consumed the spool — durable across a restart."""
    return data_dir() / "offsets" / f"{safe_name(session_id)}.spool.json"


def session_state_path(session_id: str) -> Path:
    """Session → task binding, plus the worker lockfile's neighbours."""
    return data_dir() / "state" / f"{safe_name(session_id)}.json"


def lock_path(session_id: str) -> Path:
    return data_dir() / "state" / f"{safe_name(session_id)}.lock"


def ledger_path(task_id: str) -> Path:
    return data_dir() / "ledgers" / f"{safe_name(task_id)}.json"


def ledger_dir() -> Path:
    return ensure_dir(data_dir() / "ledgers")


def index_path(workspace: str) -> Path:
    return data_dir() / "index" / f"{slug(workspace)}.jsonl"


def history_path(task_id: str) -> Path:
    """Advice history for `/second-brain-why`: append-only, one JSON per line."""
    return data_dir() / "history" / f"{safe_name(task_id)}.jsonl"


def status_path(session_id: str) -> Path:
    """What `/second-brain-stats` and the statusline read when no worker answers."""
    return data_dir() / "status" / f"{safe_name(session_id)}.json"


def status_dir() -> Path:
    return ensure_dir(data_dir() / "status")


def finish_gate_path(task_id: str) -> Path:
    """Finish-gate budget for one task: when it last fired, and how often."""
    return data_dir() / "finish-gate" / f"{safe_name(task_id)}.json"


def trigger_path(session_id: str) -> Path:
    """A human asking for a pass now, out of band (`/second-brain-run`).

    A file rather than a signal: the worker is already polling, nothing needs to
    listen, and a request that arrives while no worker runs simply waits on disk
    instead of being lost.
    """
    return data_dir() / "control" / f"{safe_name(session_id)}.run"


def control_path(task_id: str) -> Path:
    """Mutes and other user controls the worker re-reads each pass."""
    return data_dir() / "control" / f"{safe_name(task_id)}.json"


def workspace_control_path(workspace: str) -> Path:
    return data_dir() / "control" / f"ws-{slug(workspace)}.json"


def window_dump_path(session_id: str) -> Path:
    """The worker's write-through copy of its window, for `/second-brain-debug`.

    Written mechanically whenever the window changes — no model involved — so
    the command only ever reads a file, and the digest stays inspectable even
    after the worker has exited.
    """
    return data_dir() / "window" / f"{safe_name(session_id)}.md"


def config_path() -> Path:
    return data_dir() / "config.json"


def workspace_config_path(workspace: str) -> Path:
    return data_dir() / "workspaces" / f"{slug(workspace)}.json"


def oauth_state_path() -> Path:
    return data_dir() / "oauth_state.json"


def log_path() -> Path:
    return data_dir() / "second-brain.log"

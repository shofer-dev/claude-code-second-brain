"""The mailbox: one file, two readers, exactly-once delivery.

Both delivery channels read the same per-session file — the worker (pushing a
monitor line) and the drain hook (pulling at the next tool call). Running both is
safe because a reader **claims** an entry by rewriting the mailbox under an
exclusive lock and only then emits it: whichever channel gets there first wins and
the other finds nothing (DESIGN.md §Exactly-once is a file operation).

Two clocks expire an entry, and it dies on whichever comes first — the validity
TTL carried by the advisory itself (*is this still true?*) and the queue timeout
started at enqueue (*is anyone coming?*). An expired advisory is **dropped, never
delivered late**, and recorded as expired with the clock that caught it, so a
mailbox that keeps timing out shows up as a symptom rather than as silence.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from . import paths
from .advice import Advisory
from .lock import claim as claim_lock


def _load(session_id: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(paths.mailbox_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def _store(session_id: str, entries: list[dict[str, Any]]) -> None:
    paths.write_private(paths.mailbox_path(session_id),
                        json.dumps({"entries": entries}, ensure_ascii=False))


def _live(entries: list[dict[str, Any]], queue_timeout_s: float,
          now: float) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    """Split into (still deliverable, [(expired entry, which clock)])."""
    keep: list[dict[str, Any]] = []
    expired: list[tuple[dict[str, Any], str]] = []
    for entry in entries:
        advisory = Advisory.from_dict(entry.get("advisory") or {})
        if advisory is None:
            continue
        if now - float(entry.get("enqueued_at", now)) > queue_timeout_s:
            expired.append((entry, "queue_timeout"))
        elif advisory.expired(now):
            expired.append((entry, "advice_ttl"))
        else:
            keep.append(entry)
    return keep, expired


def put(session_id: str, advisory: Advisory, *, max_entries: int = 8,
        queue_timeout_s: float = 1800.0) -> None:
    """Enqueue an advisory, superseding any it replaces and trimming the oldest."""
    now = time.time()
    with claim_lock(paths.mailbox_path(session_id)) as locked:
        if not locked:
            return
        entries, _ = _live(_load(session_id), queue_timeout_s, now)
        if advisory.supersedes or advisory.dedup_key:
            entries = [
                e for e in entries
                if (e.get("advisory") or {}).get("id") != advisory.supersedes
                and ((e.get("advisory") or {}).get("dedup_key") or "\x00") != advisory.dedup_key
            ]
        entries.append({"enqueued_at": now, "advisory": advisory.to_dict()})
        _store(session_id, entries[-max_entries:])


def claim(session_id: str, *, queue_timeout_s: float = 1800.0,
          predicate: Callable[[Advisory], bool] | None = None,
          on_expire: Callable[[Advisory, str], None] | None = None) -> Advisory | None:
    """Atomically take the oldest deliverable advisory, or None.

    Never blocks: on lock contention the caller gets None, because the other
    channel is by definition mid-delivery.
    """
    now = time.time()
    with claim_lock(paths.mailbox_path(session_id)) as locked:
        if not locked:
            return None
        entries, expired = _live(_load(session_id), queue_timeout_s, now)
        if on_expire:
            for entry, clock in expired:
                advisory = Advisory.from_dict(entry.get("advisory") or {})
                if advisory is not None:
                    on_expire(advisory, clock)

        taken: Advisory | None = None
        remaining: list[dict[str, Any]] = []
        for entry in entries:
            advisory = Advisory.from_dict(entry.get("advisory") or {})
            if taken is None and advisory is not None and (predicate is None or predicate(advisory)):
                taken = advisory
                continue
            remaining.append(entry)

        if taken is not None or expired:
            _store(session_id, remaining)
        return taken


def revalidate(session_id: str, *, queue_timeout_s: float) -> int:
    """Drop entries a restarting worker must not deliver. Returns how many went.

    The mailbox survives the worker, which would otherwise let a crash at the wrong
    moment deliver an advisory minted before the restart — stale by construction,
    since the observations that would have invalidated it are the ones the crash
    lost. Persistence is a convenience for the drain hook, never a promise that an
    advisory will eventually arrive.
    """
    now = time.time()
    with claim_lock(paths.mailbox_path(session_id)) as locked:
        if not locked:
            return 0
        entries = _load(session_id)
        keep, expired = _live(entries, queue_timeout_s, now)
        if expired:
            _store(session_id, keep)
        return len(expired)


def drop_keys(session_id: str, keys: set[str]) -> int:
    """Remove undelivered advisories whose finding has since been resolved."""
    if not keys:
        return 0
    with claim_lock(paths.mailbox_path(session_id)) as locked:
        if not locked:
            return 0
        entries = _load(session_id)
        keep = [e for e in entries if ((e.get("advisory") or {}).get("dedup_key") or "") not in keys]
        if len(keep) != len(entries):
            _store(session_id, keep)
        return len(entries) - len(keep)


def peek(session_id: str) -> list[Advisory]:
    """Read without claiming — for `/second-brain-stats` and the statusline."""
    out = []
    for entry in _load(session_id):
        advisory = Advisory.from_dict(entry.get("advisory") or {})
        if advisory is not None:
            out.append(advisory)
    return out


def clear(session_id: str) -> None:
    paths.mailbox_path(session_id).unlink(missing_ok=True)

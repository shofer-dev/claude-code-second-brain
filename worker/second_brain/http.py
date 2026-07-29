"""One async JSON POST, over whichever transport this machine actually has.

A Claude Code plugin cannot assume it may install packages, and the worker has to
run on a bare `python3`. So the transport degrades in three steps, best first:

1. **`httpx`** — the async client the provider SDKs themselves use.
2. **`urllib`** on a worker thread — always present, no event-loop blocking.

The *request bodies* are built by `provider.py` against the published wire formats
either way, so nothing about which transport ran changes what is sent. Retries
live here because the fallback path has none of its own: two attempts on 429 and
5xx with a short backoff, and nothing else — a failed pass degrades to silence,
which is the design's answer to every provider problem.
"""
from __future__ import annotations

import asyncio
import json
import random
import urllib.error
import urllib.request
from typing import Any


class HttpError(RuntimeError):
    """A non-retryable HTTP failure, carrying the status for the log."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


RETRYABLE = {408, 409, 429, 500, 502, 503, 504, 529}


def _urllib_post(url: str, headers: dict[str, str], payload: dict[str, Any],
                 timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"content-type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:   # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HttpError(exc.code, exc.read().decode("utf-8", "replace")) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise HttpError(0, str(exc)) from exc


async def _once(url: str, headers: dict[str, str], payload: dict[str, Any],
                timeout: float) -> dict[str, Any]:
    try:
        import httpx
    except ImportError:
        return await asyncio.to_thread(_urllib_post, url, headers, payload, timeout)

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            raise HttpError(response.status_code, response.text)
        data: dict[str, Any] = response.json()
        return data


async def post_json(url: str, headers: dict[str, str], payload: dict[str, Any],
                    timeout: float = 120.0, attempts: int = 3) -> dict[str, Any]:
    """POST JSON, retrying transient failures. Raises HttpError on the last one."""
    last: HttpError | None = None
    for attempt in range(attempts):
        try:
            return await _once(url, headers, payload, timeout)
        except HttpError as exc:
            last = exc
            if exc.status not in RETRYABLE and exc.status != 0:
                raise
            if attempt == attempts - 1:
                raise
            await asyncio.sleep((2 ** attempt) + random.uniform(0, 0.5))
    raise last or HttpError(0, "no attempt made")

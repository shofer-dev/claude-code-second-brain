"""Talking to the model — transport only, never the conversation.

The split is deliberate (DESIGN.md §What runs the loop): **take the transport, own
the conversation.** This module builds a request against a published wire format
and parses the reply. It does not decide what is in the message array, when to
compact, or where the cache breakpoint goes — those are the whole design and they
live in `window.py` and `fork.py`.

Two wire protocols cover effectively every backend: the Anthropic Messages API
(which expresses caching as explicit `cache_control` breakpoints) and any
OpenAI-compatible `/chat/completions` endpoint (implicit prefix caching). Both
accept the same neutral, Anthropic-shaped conversation the window builds, so the
fork loop never learns which one is underneath.

The **cache breakpoint** is the one wire detail that is load-bearing rather than
incidental: everything up to it is byte-identical across every detector fork of a
pass, and marking it is what turns N full-price reads into one write and N−1 reads.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .constants import (
    ANTHROPIC_BASE_URL,
    ANTHROPIC_VERSION,
    OAUTH_BETA_HEADER,
    OPENAI_BASE_URL,
)
from .http import HttpError, post_json
from .oauth import OAuthCredential, subscription_present


class ProviderError(RuntimeError):
    """Any failure to get an answer. Callers degrade to silence, never to guessing."""


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read + self.cache_write

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens,
            self.cache_read + other.cache_read, self.cache_write + other.cache_write,
        )


@dataclass
class Reply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = ""


def _cached(block: dict[str, Any], ttl: str) -> dict[str, Any]:
    control: dict[str, Any] = {"type": "ephemeral"}
    if ttl == "1h":
        control["ttl"] = "1h"
    return {**block, "cache_control": control}


class AnthropicProvider:
    """The Anthropic Messages API, with explicit cache breakpoints."""

    wire = "anthropic"

    def __init__(self, cfg: Config) -> None:
        self.model = str(cfg.get("model.name"))
        self.base_url = (str(cfg.get("model.base_url")) or
                         os.environ.get("ANTHROPIC_BASE_URL") or ANTHROPIC_BASE_URL).rstrip("/")
        self.timeout = float(cfg.get("model.request_timeout_s", 120))
        self.cache_ttl = str(cfg.get("model.cache_ttl", "5m"))
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self._oauth = OAuthCredential() if (not self.api_key and subscription_present()) else None
        if not self.api_key and self._oauth is None:
            raise ProviderError(
                "no credentials: set ANTHROPIC_API_KEY, or log in with `claude` so the "
                "subscription token can be reused."
            )

    async def _headers(self) -> dict[str, str]:
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        betas = []
        if self.cache_ttl == "1h":
            betas.append("extended-cache-ttl-2025-04-11")
        if self._oauth is not None:
            headers["authorization"] = f"Bearer {await self._oauth.token()}"
            betas.append(OAUTH_BETA_HEADER)
        else:
            headers["x-api-key"] = self.api_key
        if betas:
            headers["anthropic-beta"] = ",".join(betas)
        return headers

    def _payload(self, system: list[dict[str, Any]], messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]], max_tokens: int,
                 cache_marks: list[tuple[int, int]]) -> dict[str, Any]:
        blocks = [dict(b) for b in system]
        if blocks:
            # The shared system prompt is stable for the task's lifetime, so it is
            # always a breakpoint; a per-fork suffix block after it stays uncached.
            blocks[0] = _cached(blocks[0], self.cache_ttl)

        msgs = [dict(m) for m in messages]
        # Mark the end of the shared prefix. Everything after a mark differs per
        # fork and must never be cached, or the forks stop sharing anything.
        # Anthropic permits four breakpoints and the system block took one.
        for message_index, block_index in cache_marks[:3]:
            if not 0 <= message_index < len(msgs):
                continue
            message = dict(msgs[message_index])
            content = message.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list) or not 0 <= block_index < len(content):
                continue
            content = [dict(b) for b in content]
            content[block_index] = _cached(content[block_index], self.cache_ttl)
            message["content"] = content
            msgs[message_index] = message

        payload: dict[str, Any] = {
            "model": self.model, "max_tokens": max_tokens,
            "system": blocks, "messages": msgs,
        }
        if tools:
            payload["tools"] = tools
        return payload

    async def send(self, system: list[dict[str, Any]], messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None, max_tokens: int = 1024,
                   cache_marks: list[tuple[int, int]] | None = None) -> Reply:
        payload = self._payload(system, messages, tools or [], max_tokens, cache_marks or [])
        try:
            data = await post_json(f"{self.base_url}/v1/messages", await self._headers(),
                                   payload, timeout=self.timeout)
        except HttpError as exc:
            raise ProviderError(str(exc)) from exc
        return _parse_anthropic(data)


def _parse_anthropic(data: dict[str, Any]) -> Reply:
    text: list[str] = []
    calls: list[ToolCall] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            calls.append(ToolCall(id=str(block.get("id", "")), name=str(block.get("name", "")),
                                  input=dict(block.get("input") or {})))
    usage = data.get("usage") or {}
    return Reply(
        text="".join(text), tool_calls=calls, stop_reason=str(data.get("stop_reason") or ""),
        usage=Usage(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_read=int(usage.get("cache_read_input_tokens", 0) or 0),
            cache_write=int(usage.get("cache_creation_input_tokens", 0) or 0),
        ),
    )


class OpenAIProvider:
    """Any OpenAI-compatible `/chat/completions` endpoint. Caching is implicit."""

    wire = "openai"

    def __init__(self, cfg: Config) -> None:
        self.model = str(cfg.get("model.name"))
        self.base_url = (str(cfg.get("model.base_url")) or
                         os.environ.get("OPENAI_BASE_URL") or OPENAI_BASE_URL).rstrip("/")
        self.timeout = float(cfg.get("model.request_timeout_s", 120))
        self.api_key = os.environ.get("OPENAI_API_KEY", "") or os.environ.get("SECOND_BRAIN_API_KEY", "")
        if not self.api_key:
            raise ProviderError("no credentials: set OPENAI_API_KEY for an OpenAI-compatible provider.")

    async def send(self, system: list[dict[str, Any]], messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None, max_tokens: int = 1024,
                   cache_marks: list[tuple[int, int]] | None = None) -> Reply:
        # `cache_marks` is deliberately ignored: this wire caches prefixes
        # implicitly, so the shared-prefix discipline still pays off — there is
        # simply nothing to declare.
        payload: dict[str, Any] = {
            "model": self.model, "max_tokens": max_tokens,
            "messages": _to_openai(system, messages),
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": {
                    "name": t["name"], "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                }} for t in tools
            ]
        try:
            data = await post_json(f"{self.base_url}/chat/completions",
                                   {"authorization": f"Bearer {self.api_key}"},
                                   payload, timeout=self.timeout)
        except HttpError as exc:
            raise ProviderError(str(exc)) from exc
        return _parse_openai(data)


def _to_openai(system: list[dict[str, Any]], messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-shaped conversation → OpenAI chat messages, losslessly enough.

    Multi-block systems become one system message: providers on this wire have no
    per-block caching to preserve, so flattening costs nothing that this wire could
    have expressed.
    """
    out: list[dict[str, Any]] = []
    joined = "\n\n".join(str(b.get("text", "")) for b in system if isinstance(b, dict))
    if joined:
        out.append({"role": "system", "content": joined})
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue
        if role == "assistant":
            text = "".join(str(b.get("text", "")) for b in content if b.get("type") == "text")
            calls = [
                {"id": b["id"], "type": "function",
                 "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))}}
                for b in content if b.get("type") == "tool_use"
            ]
            entry: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
        else:
            texts = [str(b.get("text", "")) for b in content if b.get("type") == "text"]
            for block in content:
                if block.get("type") == "tool_result":
                    out.append({"role": "tool", "tool_call_id": block.get("tool_use_id", ""),
                                "content": _stringify(block.get("content"))})
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
    return out


def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return json.dumps(content, ensure_ascii=False, default=str) if content is not None else ""


def _parse_openai(data: dict[str, Any]) -> Reply:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    calls: list[ToolCall] = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except ValueError:
            arguments = {}
        calls.append(ToolCall(id=str(call.get("id", "")), name=str(function.get("name", "")),
                              input=arguments if isinstance(arguments, dict) else {}))
    usage = data.get("usage") or {}
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                 or usage.get("prompt_cache_hit_tokens", 0) or 0)
    return Reply(
        text=str(message.get("content") or ""), tool_calls=calls,
        stop_reason=str(choice.get("finish_reason") or ""),
        usage=Usage(input_tokens=max(0, int(usage.get("prompt_tokens", 0) or 0) - cached),
                    output_tokens=int(usage.get("completion_tokens", 0) or 0),
                    cache_read=cached),
    )


def make_provider(cfg: Config) -> AnthropicProvider | OpenAIProvider:
    if str(cfg.get("model.provider", "anthropic")) == "openai":
        return OpenAIProvider(cfg)
    return AnthropicProvider(cfg)

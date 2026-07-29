"""The MCP adapter: grants, name mangling, isolation, and honest unavailability.

The protocol itself is the official SDK's job, so what is tested here is the part
this plugin owns — that a detector reaches exactly what it was granted and nothing
else, that results come back as text a fork can carry, and that a missing SDK is
*reported* rather than quietly shrinking a detector's reach.
"""
from __future__ import annotations

import asyncio

import pytest

from second_brain.mcpclient import McpHub, mangle, unmangle
from second_brain.tools import ToolError, Toolbox, parse_grants


class FakeTool:
    def __init__(self, name, schema=None):
        self.name = name
        self.description = f"does {name}"
        self.inputSchema = schema or {"type": "object", "properties": {"q": {"type": "string"}}}


class FakeResult:
    def __init__(self, text, is_error=False):
        self.content = [type("Block", (), {"type": "text", "text": text})()]
        self.isError = is_error


class FakeSession:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "boom":
            return FakeResult("upstream exploded", is_error=True)
        return FakeResult(f"{name} says hello")


def hub_with(server="code-search", tools=("query", "index")):
    hub = McpHub({server: {"command": "x"}})
    hub.available = True
    entry = hub.servers[server]
    entry.session = FakeSession()
    entry.tools = [{"name": t, "description": f"does {t}",
                    "input_schema": FakeTool(t).inputSchema} for t in tools]
    return hub


# ── names ───────────────────────────────────────────────────────────────────
def test_names_round_trip():
    assert mangle("code-search", "query") == "mcp__code-search__query"
    assert unmangle("mcp__code-search__query") == ("code-search", "query")
    with pytest.raises(ToolError):
        unmangle("Read")
    with pytest.raises(ToolError):
        unmangle("mcp__broken")


# ── grants decide what is even offered ──────────────────────────────────────
def test_a_whole_server_grant_offers_all_of_its_tools():
    names = {t["name"] for t in hub_with().definitions(parse_grants([{"mcp": "code-search"}]))}
    assert names == {"mcp__code-search__query", "mcp__code-search__index"}


def test_a_single_tool_grant_offers_only_that_tool():
    grant = parse_grants(["mcp__code-search__query"])
    assert [t["name"] for t in hub_with().definitions(grant)] == ["mcp__code-search__query"]


def test_no_grant_offers_nothing():
    assert hub_with().definitions(parse_grants(["Read"])) == []


def test_a_disconnected_server_offers_nothing():
    hub = hub_with()
    hub.servers["code-search"].session = None
    assert hub.definitions(parse_grants([{"mcp": "code-search"}])) == []


# ── dispatch re-checks the grant ────────────────────────────────────────────
def test_dispatch_refuses_a_tool_the_detector_was_never_offered(tmp_path):
    hub = hub_with()
    box = Toolbox(tmp_path, mcp=hub)
    grant = parse_grants(["mcp__code-search__query"])
    ok = asyncio.run(box.dispatch("mcp__code-search__query", {"q": "health"}, grant))
    refused = asyncio.run(box.dispatch("mcp__code-search__index", {}, grant))
    assert "query says hello" in ok
    assert "not available to this detector" in refused
    assert hub.servers["code-search"].session.calls == [("query", {"q": "health"})]


def test_an_upstream_error_comes_back_as_text_not_an_exception(tmp_path):
    hub = hub_with(tools=("boom",))
    box = Toolbox(tmp_path, mcp=hub)
    result = asyncio.run(box.dispatch("mcp__code-search__boom", {},
                                      parse_grants([{"mcp": "code-search"}])))
    assert result.startswith("error: upstream exploded")


def test_an_unknown_server_is_refused(tmp_path):
    box = Toolbox(tmp_path, mcp=hub_with())
    result = asyncio.run(box.dispatch("mcp__nope__x", {}, parse_grants([{"mcp": "nope"}])))
    assert "no such MCP server" in result


# ── unavailability is reported, never silently absorbed ─────────────────────
def test_a_missing_sdk_is_reported_rather_than_silently_reducing_reach(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "mcp":
            raise ImportError("no module named mcp")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    hub = McpHub({"code-search": {"command": "x"}})
    asyncio.run(hub.start())
    assert hub.available is False
    assert "pip install mcp" in hub.reason
    assert hub.status()["reason"] == hub.reason
    # And the tools are absent rather than stubbed: a detector that needed one
    # produces no answer instead of a confident answer built without it.
    assert hub.definitions(parse_grants([{"mcp": "code-search"}])) == []


def test_no_configured_servers_means_no_work_at_all():
    hub = McpHub({})
    asyncio.run(hub.start())
    assert hub.servers == {} and hub.available is False


# ── transport failures leave through one door ───────────────────────────────
def test_every_transport_failure_becomes_an_http_error():
    """A provider that is simply unreachable must degrade, not raise a traceback.

    `httpx` raises its own exception hierarchy, which is neither `HttpError` nor
    `ProviderError` — so without the mapping a connection refusal escapes the retry
    logic and the fork's error handling, and surfaces as an unhandled exception in
    the observer loop. Caught by running the real worker against a dead address.
    """
    # `_once`, not `post_json`: the conftest fixture disables the latter so that no
    # test can reach a provider. This one deliberately dials a dead local port.
    from second_brain.http import HttpError, _once

    with pytest.raises(HttpError) as caught:
        asyncio.run(_once("http://127.0.0.1:1/v1/messages", {}, {"x": 1}, 1.0))
    assert caught.value.status == 0
    assert "Error" in str(caught.value)


def test_a_provider_failure_reaches_the_fork_as_a_provider_error(monkeypatch):
    from second_brain import http, provider
    from second_brain.config import Config

    async def refuse(*_a, **_k):
        raise http.HttpError(0, "ConnectError: all connection attempts failed")

    monkeypatch.setattr(http, "post_json", refuse)
    monkeypatch.setattr(provider, "post_json", refuse)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    client = provider.AnthropicProvider(Config.load(None))
    with pytest.raises(provider.ProviderError):
        asyncio.run(client.send(system=[{"type": "text", "text": "s"}],
                                messages=[{"role": "user", "content": "hi"}]))

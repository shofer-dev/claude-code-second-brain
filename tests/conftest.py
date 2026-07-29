"""Test fixtures: an isolated data directory and the worker on the import path.

Every test runs against a throwaway `CLAUDE_PLUGIN_DATA`, so nothing here can read
or write a real session's spool, mailbox or ledgers.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "hooks"))


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Point the whole plugin at a temporary state root for the duration of a test."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "state"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from second_brain import paths
    importlib.reload(paths)
    return tmp_path


@pytest.fixture(autouse=True)
def no_real_provider(monkeypatch):
    """No test may reach a model provider, and none may touch the real credentials.

    Without this a component that falls back to `make_provider()` would pick up the
    machine's subscription token and send a synthetic window to the API. Tests that
    want a model inject a fake one explicitly.
    """
    from second_brain import http, oauth, provider

    def refuse(_cfg):
        raise provider.ProviderError("provider disabled in tests")

    async def no_network(*_args, **_kwargs):
        raise http.HttpError(0, "network disabled in tests")

    monkeypatch.setattr(provider, "make_provider", refuse)
    monkeypatch.setattr(http, "post_json", no_network)
    monkeypatch.setattr(oauth, "credentials_path", lambda: Path("/nonexistent/.credentials.json"))
    monkeypatch.setattr(oauth, "subscription_present", lambda: False)


@pytest.fixture
def cfg():
    from second_brain.config import Config
    return Config.load(None)


@pytest.fixture
def projection_cfg(cfg):
    return cfg.group("projection")

"""Zero-config authentication: reuse the Claude Code subscription, if it is there.

The plugin should work the moment it is installed, with no key to paste, so when
`~/.claude/.credentials.json` holds a subscription token the observer runs on it
(bearer token plus the oauth beta header). An API key in the environment always
wins; this is the fallback that makes "install and it works" true.

Two properties worth stating plainly:

- **It draws on the subscription's rate-limit budget**, not on metered API
  billing. PRIVACY.md says so; a user who wants the observer on separate billing
  sets `ANTHROPIC_API_KEY` and this path is never taken.
- **Refreshed tokens are persisted to the plugin's own data dir**, never written
  back to Claude Code's credentials file — corrupting the harness's own auth in
  order to watch it would be an unusually bad trade.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import paths
from .constants import OAUTH_CLIENT_ID, OAUTH_REFRESH_MARGIN_MS, OAUTH_TOKEN_ENDPOINT
from .http import post_json


def credentials_path() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def subscription_present() -> bool:
    try:
        data = json.loads(credentials_path().read_text(encoding="utf-8"))
        return bool(data.get("claudeAiOauth", {}).get("accessToken"))
    except (OSError, ValueError, AttributeError):
        return False


class OAuthCredential:
    """Holds and refreshes the subscription token; `await token()` is always valid."""

    def __init__(self, state_path: Path | None = None) -> None:
        self._state_path = state_path or paths.oauth_state_path()
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.expires_at: int = 0
        self._load()

    def _load(self) -> None:
        """Take the later-expiring of our own refreshed state and Claude Code's."""
        for path in (self._state_path, credentials_path()):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            record = raw.get("claudeAiOauth", raw) if isinstance(raw, dict) else {}
            token = record.get("accessToken")
            expires = int(record.get("expiresAt", 0) or 0)
            if token and expires >= self.expires_at:
                self.access_token = token
                self.refresh_token = record.get("refreshToken")
                self.expires_at = expires

    def _persist(self) -> None:
        try:
            paths.write_private(self._state_path, json.dumps({
                "accessToken": self.access_token,
                "refreshToken": self.refresh_token,
                "expiresAt": self.expires_at,
            }))
        except OSError:
            pass

    async def _refresh(self) -> None:
        if not self.refresh_token:
            raise RuntimeError(
                "second-brain: no subscription token to refresh — run `claude` to log in, "
                "or set ANTHROPIC_API_KEY."
            )
        data = await post_json(OAUTH_TOKEN_ENDPOINT, {}, {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        }, timeout=30.0)
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.expires_at = int(time.time() * 1000) + int(data.get("expires_in", 3600)) * 1000
        self._persist()

    async def token(self) -> str:
        now_ms = int(time.time() * 1000)
        if not self.access_token or now_ms >= self.expires_at - OAUTH_REFRESH_MARGIN_MS:
            self._load()        # Claude Code may have refreshed it for us already
            if not self.access_token or now_ms >= self.expires_at - OAUTH_REFRESH_MARGIN_MS:
                await self._refresh()
        assert self.access_token is not None
        return self.access_token

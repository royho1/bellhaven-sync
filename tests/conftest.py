from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bellhaven_sync import config, crm_client

FAKE_TOKEN = "test-token-abcdef0123456789"


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload: Any = None, status_code: int = 200, text: str | None = None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else ""

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """A session that only knows how to GET, which is the point."""

    def __init__(self, responses: list[FakeResponse] | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: Any = None) -> FakeResponse:
        self.calls.append({"url": url, "params": params or {}, "timeout": timeout})
        if not self.responses:
            raise AssertionError(f"unexpected extra GET to {url}")
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch, tmp_path: Path):
    config.reset_settings()
    crm_client.reset_session()
    monkeypatch.setenv("CLIPBOARD_API_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("CLIPBOARD_API_BASE_URL", "https://crm.example.test/api/v1")
    monkeypatch.setenv("BELLHAVEN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("BELLHAVEN_PARENT_ACCOUNT_ID", raising=False)
    yield
    config.reset_settings()
    crm_client.reset_session()


@pytest.fixture
def settings(clean_settings):
    return config.load_settings(env_file=Path("/nonexistent/.env"), force=True)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(crm_client.time, "sleep", lambda _seconds: None)

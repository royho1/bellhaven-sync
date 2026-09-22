"""Settings, the shared HTTP session factory, and secret redaction.

The API token is read from the environment only. It is never written to disk,
never included in a log record, and never included in an exception message:
anything that might carry it goes through `redact()` first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv

DEFAULT_BASE_URL = "https://analyst-assessment-production.up.railway.app/api/v1"

# (connect, read) seconds. No documented rate limit exists, so requests are
# sequential rather than throttled to an invented number.
DEFAULT_TIMEOUT = (10.0, 30.0)

REDACTION_PLACEHOLDER = "***REDACTED***"

REPO_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


@dataclass(frozen=True)
class Settings:
    api_token: str
    base_url: str
    dry_run: bool
    bellhaven_parent_account_id: str | None
    data_dir: Path

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshots"


_secrets: set[str] = set()
_settings: Settings | None = None


def register_secret(value: str | None) -> None:
    """Add a value that `redact()` must scrub out of any string."""
    if value and len(value) >= 8:
        _secrets.add(value)


def redact(text: object) -> str:
    """Return `text` as a string with every registered secret replaced."""
    result = text if isinstance(text, str) else repr(text)
    for secret in _secrets:
        result = result.replace(secret, REDACTION_PLACEHOLDER)
    return result


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(*, env_file: Path | None = None, force: bool = False) -> Settings:
    """Load settings from the environment, falling back to a local .env file."""
    global _settings
    if _settings is not None and not force:
        return _settings

    load_dotenv(dotenv_path=env_file or (REPO_ROOT / ".env"), override=False)

    token = (os.environ.get("CLIPBOARD_API_TOKEN") or "").strip()
    if not token:
        raise ConfigError(
            "CLIPBOARD_API_TOKEN is not set. Copy .env.example to .env and put the "
            "token there, or export it in your shell. The token is never committed."
        )
    register_secret(token)

    base_url = (os.environ.get("CLIPBOARD_API_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    parent_id = (os.environ.get("BELLHAVEN_PARENT_ACCOUNT_ID") or "").strip() or None
    data_dir = Path(os.environ.get("BELLHAVEN_DATA_DIR") or (REPO_ROOT / "data"))

    _settings = Settings(
        api_token=token,
        base_url=base_url,
        dry_run=_env_flag("DRY_RUN", True),
        bellhaven_parent_account_id=parent_id,
        data_dir=data_dir,
    )
    return _settings


def reset_settings() -> None:
    """Drop cached settings and registered secrets. Used by tests."""
    global _settings
    _settings = None
    _secrets.clear()


def build_session(settings: Settings) -> requests.Session:
    """Create a session carrying the bearer token.

    Shared by the read client and, later, by the apply module, so that both
    speak to the API the same way. The session itself imposes no method limits;
    GET-only is enforced by `crm_client` never calling anything else.
    """
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {settings.api_token}",
            "Accept": "application/json",
            "User-Agent": "bellhaven-sync/0.1 (+local reconciliation tool)",
        }
    )
    return session

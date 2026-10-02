"""Public scrape must not require CRM credentials; CRM discover still must."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bellhaven_sync import config
from bellhaven_sync.cli import cmd_scrape
from tests.test_scraper import BASE, fixture_fetcher


def test_load_local_paths_does_not_require_crm_token(monkeypatch, tmp_path: Path):
    config.reset_settings()
    monkeypatch.delenv("CLIPBOARD_API_TOKEN", raising=False)
    monkeypatch.setenv("BELLHAVEN_DATA_DIR", str(tmp_path / "site-data"))

    paths = config.load_local_paths(env_file=tmp_path / "absent.env")

    assert paths.data_dir == tmp_path / "site-data"
    assert paths.scrape_dir == tmp_path / "site-data" / "scrapes"


def test_load_settings_still_requires_crm_token(monkeypatch, tmp_path: Path):
    config.reset_settings()
    monkeypatch.delenv("CLIPBOARD_API_TOKEN", raising=False)
    monkeypatch.setenv("BELLHAVEN_DATA_DIR", str(tmp_path / "data"))

    with pytest.raises(config.ConfigError, match="CLIPBOARD_API_TOKEN"):
        config.load_settings(env_file=tmp_path / "absent.env", force=True)


def test_scrape_cli_runs_without_crm_token(monkeypatch, tmp_path: Path):
    config.reset_settings()
    monkeypatch.delenv("CLIPBOARD_API_TOKEN", raising=False)
    monkeypatch.setenv("BELLHAVEN_DATA_DIR", str(tmp_path / "data"))

    homepage = (
        Path(__file__).parent / "fixtures" / "html" / "homepage.html"
    ).read_text(encoding="utf-8").replace("35 communities", "5 communities")
    monkeypatch.setattr(
        "bellhaven_sync.scraper.default_fetcher",
        lambda data_dir=None: fixture_fetcher({f"{BASE}/": homepage}),
    )

    # discover-style CRM settings must still fail in this environment
    with pytest.raises(config.ConfigError, match="CLIPBOARD_API_TOKEN"):
        config.load_settings(env_file=tmp_path / "absent.env", force=True)

    exit_code = cmd_scrape(
        SimpleNamespace(base_url=BASE, urls_only=False)
    )

    # Fixture homepage claim matches union count, so enrichment can complete.
    assert exit_code == 0
    scrapes = list((tmp_path / "data" / "scrapes").glob("facilities-*.json"))
    assert scrapes, "scrape should write a local facility snapshot without a CRM token"


@pytest.mark.parametrize("raw", [None, "", "true", "TRUE", "1", "yes", "on"])
def test_dry_run_is_on_by_default_and_for_true_values(monkeypatch, tmp_path: Path, raw):
    if raw is None:
        monkeypatch.delenv("DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("DRY_RUN", raw)
    assert config.load_settings(env_file=tmp_path / "absent.env", force=True).dry_run is True


@pytest.mark.parametrize("raw", ["false", "False", " 0 ", "no", "off"])
def test_dry_run_turns_off_only_for_explicit_false_values(monkeypatch, tmp_path: Path, raw):
    monkeypatch.setenv("DRY_RUN", raw)
    assert config.load_settings(env_file=tmp_path / "absent.env", force=True).dry_run is False


@pytest.mark.parametrize("raw", ["ture", "flase", "disabled", "maybe"])
def test_unrecognized_dry_run_value_keeps_dry_run_on(monkeypatch, tmp_path: Path, raw, caplog):
    monkeypatch.setenv("DRY_RUN", raw)
    with caplog.at_level("WARNING", logger="bellhaven_sync.config"):
        settings = config.load_settings(env_file=tmp_path / "absent.env", force=True)
    assert settings.dry_run is True
    assert "Unrecognized DRY_RUN value" in caplog.text

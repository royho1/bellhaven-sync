"""End-to-end read-only sync path with fixtures (no network)."""

from __future__ import annotations

from pathlib import Path

from bellhaven_sync import fields, pipeline
from bellhaven_sync.store import ProposalStore
from tests.test_scraper import fixture_fetcher

BASE = "https://www.bellhavenseniorliving.com"
FIXTURES = Path(__file__).parent / "fixtures" / "html"


def _accounts():
    parent = {
        fields.ACCOUNT_ID: "PARENT",
        fields.NAME: "Bellhaven Senior Living (Parent Account)",
        fields.PARENT_ID: "",
        fields.STREET: "",
        fields.CITY: "",
        fields.STATE: "",
        fields.ZIP: "",
        fields.STATUS: "Active",
        fields.LIFETIME_REVENUE: 0,
        fields.OUTSTANDING_AR: 0,
    }
    # Match Findlay fixture address so sync produces a confident match.
    findlay = {
        fields.ACCOUNT_ID: "FINDLAY",
        fields.NAME: "Bellhaven Meadows of Findlay",
        fields.PARENT_ID: "PARENT",
        fields.STREET: "1800 N Blanchard St",
        fields.CITY: "Findlay",
        fields.STATE: "OH",
        fields.ZIP: "45840",
        fields.CARE_TYPE: "Assisted Living",
        fields.PHONE: "",
        fields.STATUS: "Active",
        fields.LIFETIME_REVENUE: 0,
        fields.OUTSTANDING_AR: 0,
    }
    return [parent, findlay]


def test_run_sync_persists_proposals(settings, tmp_path):
    homepage = (FIXTURES / "homepage.html").read_text(encoding="utf-8").replace(
        "35 communities", "5 communities"
    )
    db = tmp_path / "sync.sqlite"
    result = pipeline.run_sync(
        settings,
        accounts=_accounts(),
        fetch=fixture_fetcher({f"{BASE}/": homepage}),
        base_url=BASE,
        db_path=db,
        enrich_pages=True,
    )
    assert result.run_id >= 1
    assert result.parent_resolved is True
    assert result.match_count >= 1
    store = ProposalStore(db)
    assert store.list_proposals(run_id=result.run_id)
    summary = pipeline.format_sync_summary(result)
    assert "No CRM writes were attempted." in summary
    assert "Matched:" in summary


def _stub_sync(monkeypatch, tmp_path, blockers):
    from types import SimpleNamespace

    from bellhaven_sync.proposals import ProposalBatch

    batch = ProposalBatch(proposals=[], scrape_complete=not blockers, parent_account_id="PARENT")
    batch.blockers = list(blockers)
    result = pipeline.SyncResult(
        run_id=1,
        batch=batch,
        match_count=0,
        account_count=0,
        facility_count=0,
        parent_resolved=True,
        db_path=tmp_path / "db.sqlite",
    )
    monkeypatch.setattr(pipeline, "run_sync", lambda *_args, **_kwargs: result)
    return SimpleNamespace(base_url=BASE, db=None, urls_only=False)


def test_cli_sync_exits_zero_without_blockers(settings, tmp_path, monkeypatch):
    from bellhaven_sync.cli import cmd_sync

    assert cmd_sync(_stub_sync(monkeypatch, tmp_path, [])) == 0


def test_cli_sync_exits_three_when_the_run_has_blockers(settings, tmp_path, monkeypatch, capsys):
    from bellhaven_sync.cli import cmd_sync

    args = _stub_sync(monkeypatch, tmp_path, ["Scrape incomplete: 34 of 35 communities found."])
    assert cmd_sync(args) == 3
    assert "BLOCKER: Scrape incomplete" in capsys.readouterr().out

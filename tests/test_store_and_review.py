"""SQLite persistence and local review status transitions."""

from __future__ import annotations

from bellhaven_sync.proposals import (
    ACTION_UPDATE_FIELDS,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    Proposal,
    ProposalBatch,
)
from bellhaven_sync.review_app import create_app
from bellhaven_sync.store import ProposalStore


def _batch(*proposals: Proposal) -> ProposalBatch:
    batch = ProposalBatch(
        proposals=list(proposals),
        blockers=[],
        summary={"proposals": len(proposals)},
        scrape_complete=True,
        parent_account_id="PARENT",
        generated_at="2026-01-01T00:00:00+00:00",
    )
    return batch


def test_save_run_does_not_overwrite_prior_review_history(tmp_path):
    db = tmp_path / "bellhaven_sync.db"
    store = ProposalStore(db)
    first = Proposal(
        action_type=ACTION_UPDATE_FIELDS,
        account_id="A1",
        facility_url="https://example.test/a",
        current_values={"phone": "1"},
        proposed_values={"phone": "2"},
        evidence={"reason": "first run"},
        confidence="high",
    )
    run1 = store.save_run(_batch(first))
    stored = store.list_proposals(run_id=run1)[0]
    store.set_status(stored.id, STATUS_APPROVED)

    second = Proposal(
        action_type=ACTION_UPDATE_FIELDS,
        account_id="A1",
        facility_url="https://example.test/a",
        current_values={"phone": "1"},
        proposed_values={"phone": "3"},
        evidence={"reason": "second run"},
        confidence="high",
    )
    run2 = store.save_run(_batch(second))
    assert run2 != run1

    prior = store.get_proposal(stored.id)
    assert prior is not None
    assert prior.status == STATUS_APPROVED
    assert prior.evidence["reason"] == "first run"

    latest = store.list_proposals(run_id=run2)
    assert len(latest) == 1
    assert latest[0].status == STATUS_PENDING


def test_approve_and_reject_persist(tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    run_id = store.save_run(
        _batch(
            Proposal(
                action_type=ACTION_UPDATE_FIELDS,
                account_id="A1",
                facility_url="https://example.test/a",
                current_values={},
                proposed_values={"name": "X"},
                evidence={"reason": "diff"},
                confidence="high",
            )
        )
    )
    proposal = store.list_proposals(run_id=run_id)[0]
    assert store.set_status(proposal.id, STATUS_APPROVED).status == STATUS_APPROVED
    assert store.set_status(proposal.id, STATUS_REJECTED).status == STATUS_REJECTED


def test_review_ui_approve_updates_sqlite_only(tmp_path):
    db = tmp_path / "ui.sqlite"
    store = ProposalStore(db)
    store.save_run(
        _batch(
            Proposal(
                action_type=ACTION_UPDATE_FIELDS,
                account_id="A1",
                facility_url="https://example.test/a",
                current_values={"city": "Old"},
                proposed_values={"city": "New"},
                evidence={"reason": "city differs"},
                confidence="high",
            )
        )
    )
    proposal = store.list_proposals()[0]
    app = create_app(db)
    client = app.test_client()

    listing = client.get("/")
    assert listing.status_code == 200
    assert b"city differs" in listing.data
    assert b"Viewing reconciliation run" in listing.data

    response = client.post(
        f"/proposals/{proposal.id}/status",
        data={"status": "approved", "return_run_id": str(proposal.run_id)},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert store.get_proposal(proposal.id).status == STATUS_APPROVED


def test_review_ui_defaults_to_latest_run_only(tmp_path):
    db = tmp_path / "runs.sqlite"
    store = ProposalStore(db)
    run1 = store.save_run(
        _batch(
            Proposal(
                action_type=ACTION_UPDATE_FIELDS,
                account_id="OLD",
                facility_url="https://example.test/old",
                current_values={"city": "OldCity"},
                proposed_values={"city": "OldProposed"},
                evidence={"reason": "old-run-pending"},
                confidence="high",
            )
        )
    )
    run2 = store.save_run(
        _batch(
            Proposal(
                action_type=ACTION_UPDATE_FIELDS,
                account_id="NEW",
                facility_url="https://example.test/new",
                current_values={"city": "NewCity"},
                proposed_values={"city": "NewProposed"},
                evidence={"reason": "new-run-pending"},
                confidence="high",
            )
        )
    )
    assert store.latest_run_id() == run2

    app = create_app(db)
    client = app.test_client()

    default = client.get("/")
    assert default.status_code == 200
    assert b"new-run-pending" in default.data
    assert b"old-run-pending" not in default.data
    assert f"#{run2}".encode() in default.data or b"(latest)" in default.data

    filtered = client.get("/?status=pending")
    assert b"new-run-pending" in filtered.data
    assert b"old-run-pending" not in filtered.data

    historical = client.get(f"/?run_id={run1}")
    assert historical.status_code == 200
    assert b"old-run-pending" in historical.data
    assert b"new-run-pending" not in historical.data
    assert b"(historical)" in historical.data

    old_proposal = store.list_proposals(run_id=run1)[0]
    approved = client.post(
        f"/proposals/{old_proposal.id}/status",
        data={
            "status": "approved",
            "return_run_id": str(run1),
            "return_status": "",
        },
        follow_redirects=True,
    )
    assert approved.status_code == 200
    assert store.get_proposal(old_proposal.id).status == STATUS_APPROVED
    assert b"Viewing reconciliation run" in approved.data
    assert b"#1" in approved.data or b"run #1" in approved.data.lower() or b"(historical)" in approved.data
    assert b"new-run-pending" not in approved.data
    assert b"old-run-pending" in approved.data

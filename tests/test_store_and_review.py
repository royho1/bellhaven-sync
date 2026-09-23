"""SQLite persistence and local review status transitions."""

from __future__ import annotations

import re

import pytest

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


def _csrf_token(html: bytes) -> str:
    match = re.search(rb'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token missing from review page"
    return match.group(1).decode()


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
    token = _csrf_token(listing.data)

    response = client.post(
        f"/proposals/{proposal.id}/status",
        data={
            "status": "approved",
            "return_run_id": str(proposal.run_id),
            "csrf_token": token,
        },
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
            "csrf_token": _csrf_token(historical.data),
        },
        follow_redirects=True,
    )
    assert approved.status_code == 200
    assert store.get_proposal(old_proposal.id).status == STATUS_APPROVED
    assert b"Viewing reconciliation run" in approved.data
    assert b"#1" in approved.data or b"run #1" in approved.data.lower() or b"(historical)" in approved.data
    assert b"new-run-pending" not in approved.data
    assert b"old-run-pending" in approved.data


def test_status_post_requires_valid_csrf_token(tmp_path):
    db = tmp_path / "csrf.sqlite"
    store = ProposalStore(db)
    run_id = store.save_run(
        _batch(
            Proposal(
                action_type=ACTION_UPDATE_FIELDS,
                account_id="A1",
                facility_url="https://example.test/a",
                current_values={"city": "Old"},
                proposed_values={"city": "New"},
                evidence={"reason": "csrf-target"},
                confidence="high",
            )
        )
    )
    proposal = store.list_proposals(run_id=run_id)[0]
    app = create_app(db)
    client = app.test_client()
    page = client.get(f"/?run_id={run_id}&status=pending&action_type=update_fields")
    token = _csrf_token(page.data)

    missing = client.post(
        f"/proposals/{proposal.id}/status",
        data={
            "status": "approved",
            "return_run_id": str(run_id),
            "return_status": "pending",
            "return_action_type": "update_fields",
        },
    )
    assert missing.status_code == 403
    assert store.get_proposal(proposal.id).status == STATUS_PENDING

    wrong = client.post(
        f"/proposals/{proposal.id}/status",
        data={
            "status": "approved",
            "return_run_id": str(run_id),
            "return_status": "pending",
            "return_action_type": "update_fields",
            "csrf_token": "not-the-session-token",
        },
    )
    assert wrong.status_code == 403
    assert store.get_proposal(proposal.id).status == STATUS_PENDING

    rejected = client.post(
        f"/proposals/{proposal.id}/status",
        data={
            "status": "rejected",
            "return_run_id": str(run_id),
            "return_status": "pending",
            "return_action_type": "update_fields",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert rejected.status_code == 302
    location = rejected.headers["Location"]
    assert f"run_id={run_id}" in location
    assert "status=pending" in location
    assert "action_type=update_fields" in location
    assert store.get_proposal(proposal.id).status == STATUS_REJECTED


def test_serve_rejects_non_loopback_hosts(tmp_path, capsys):
    from bellhaven_sync.cli import main
    from bellhaven_sync.review_app import LoopbackHostError, normalize_loopback_host, run_server

    assert normalize_loopback_host("127.0.0.1") == "127.0.0.1"
    assert normalize_loopback_host("localhost") == "localhost"
    assert normalize_loopback_host("::1") == "::1"
    assert normalize_loopback_host("[::1]") == "::1"

    db = tmp_path / "local.sqlite"
    for host in ("0.0.0.0", "192.168.1.20", "example.com"):
        with pytest.raises(LoopbackHostError):
            run_server(db, host=host)

    code = main(["serve", "--host", "0.0.0.0", "--db", str(db)])
    assert code == 2
    captured = capsys.readouterr()
    assert "loopback" in captured.err.lower()
    assert "0.0.0.0" in captured.err


def test_legacy_create_identity_locks_migrate_before_structured_index(tmp_path):
    """Existing DBs with opaque identity_key locks must initialize without OperationalError."""
    import sqlite3

    db_path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE reconciliation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            scrape_complete INTEGER NOT NULL DEFAULT 0,
            parent_account_id TEXT,
            blockers_json TEXT NOT NULL DEFAULT '[]',
            summary_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            account_id TEXT,
            facility_url TEXT,
            current_values_json TEXT NOT NULL,
            proposed_values_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            confidence TEXT NOT NULL,
            requires_review INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES reconciliation_runs(id)
        );
        CREATE TABLE application_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id INTEGER NOT NULL,
            run_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            mode TEXT NOT NULL,
            state TEXT NOT NULL,
            created_account_id TEXT,
            error_text TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            FOREIGN KEY (proposal_id) REFERENCES proposals(id)
        );
        CREATE TABLE create_identity_locks (
            identity_key TEXT PRIMARY KEY,
            proposal_id INTEGER NOT NULL,
            attempt_id INTEGER NOT NULL,
            claimed_at TEXT NOT NULL,
            FOREIGN KEY (proposal_id) REFERENCES proposals(id),
            FOREIGN KEY (attempt_id) REFERENCES application_attempts(id)
        );
        INSERT INTO reconciliation_runs (
            started_at, finished_at, scrape_complete, parent_account_id,
            blockers_json, summary_json
        ) VALUES (
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:01:00+00:00', 1, 'PARENT',
            '[]', '{}'
        );
        INSERT INTO proposals (
            run_id, action_type, account_id, facility_url,
            current_values_json, proposed_values_json, evidence_json,
            confidence, requires_review, status, created_at, updated_at
        ) VALUES (
            1, 'update_fields', 'A1', 'https://example.test/a',
            '{"phone":"1"}', '{"phone":"2"}', '{"reason":"legacy"}',
            'high', 1, 'approved',
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
        );
        """
    )
    conn.commit()
    conn.close()

    store = ProposalStore(db_path)

    with store.connect() as conn:
        cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(create_identity_locks)")}
        indexes = {
            str(row["name"])
            for row in conn.execute("PRAGMA index_list(create_identity_locks)").fetchall()
        }
        proposal = conn.execute(
            "SELECT status, evidence_json FROM proposals WHERE id = 1"
        ).fetchone()
        runs = conn.execute("SELECT COUNT(*) AS n FROM reconciliation_runs").fetchone()

    assert "identity_key" not in cols
    assert {
        "parent_id",
        "name_norm",
        "street_norm",
        "state_norm",
        "city_norm",
        "zip_norm",
        "proposal_id",
        "attempt_id",
        "claimed_at",
    } <= cols
    assert "idx_create_identity_locks_core" in indexes
    assert proposal is not None
    assert proposal["status"] == "approved"
    assert "legacy" in proposal["evidence_json"]
    assert int(runs["n"]) == 1

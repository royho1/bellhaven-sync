"""Apply path: approved proposals only, fake HTTP, no live CRM writes."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
import requests

from bellhaven_sync import fields
from bellhaven_sync.apply import ExecuteWhileDryRunError, run_apply
from bellhaven_sync.apply_cli import main as apply_main
from bellhaven_sync.proposals import (
    ACTION_CHOW,
    ACTION_CREATE_ACCOUNT,
    ACTION_REPARENT,
    ACTION_REVIEW_AMBIGUOUS,
    ACTION_UPDATE_FIELDS,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    Proposal,
    ProposalBatch,
)
from bellhaven_sync.store import (
    CLAIM_ALREADY_APPLIED,
    CLAIM_CLAIMED,
    CLAIM_IN_PROGRESS,
    CLAIM_NOT_APPROVED,
    MODE_DRY_RUN,
    MODE_EXECUTE,
    STATE_APPLIED,
    STATE_BLOCKED,
    STATE_FAILED,
    STATE_IN_PROGRESS,
    STATE_PLANNED,
    STATE_UNCERTAIN,
    ProposalStore,
)
from tests.conftest import FAKE_TOKEN, FakeResponse


class CrmFake:
    def __init__(self, accounts: dict[str, dict[str, Any]]):
        self.accounts = accounts
        self.posts: list[dict[str, Any]] = []
        self.patches: list[tuple[str, dict[str, Any]]] = []
        self.post_failures = 0
        self.patch_failures = 0
        self.lose_post_response = 0
        self.lose_patch_response = 0
        self.list_error: Exception | None = None
        self._seq = 1

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: Any = None) -> FakeResponse:
        path = url.rstrip("/")
        if path.endswith("/accounts"):
            if self.list_error is not None:
                raise self.list_error
            return FakeResponse({"data": list(self.accounts.values())})
        account_id = path.rsplit("/", 1)[-1]
        account = self.accounts.get(account_id)
        if account is None:
            return FakeResponse({"missing": True}, status_code=404, text="missing")
        return FakeResponse(account)

    def post(self, url: str, json: dict[str, Any] | None = None, timeout: Any = None) -> FakeResponse:
        body = dict(json or {})
        self.posts.append(body)
        if self.post_failures:
            self.post_failures -= 1
            raise requests.ConnectionError(f"post dropped {FAKE_TOKEN}")
        account_id = f"NEW{self._seq}"
        self._seq += 1
        created = {**body, fields.ACCOUNT_ID: account_id}
        self.accounts[account_id] = created
        if self.lose_post_response:
            self.lose_post_response -= 1
            raise requests.Timeout(f"post response lost {FAKE_TOKEN}")
        return FakeResponse(created)

    def patch(self, url: str, json: dict[str, Any] | None = None, timeout: Any = None) -> FakeResponse:
        body = dict(json or {})
        account_id = url.rstrip("/").rsplit("/", 1)[-1]
        self.patches.append((account_id, body))
        if self.patch_failures:
            self.patch_failures -= 1
            raise requests.ConnectionError(f"patch dropped {FAKE_TOKEN}")
        self.accounts[account_id].update(body)
        if self.lose_patch_response:
            self.lose_patch_response -= 1
            raise requests.Timeout(f"patch response lost {FAKE_TOKEN}")
        return FakeResponse(self.accounts[account_id])


def _batch(*proposals: Proposal) -> ProposalBatch:
    return ProposalBatch(
        proposals=list(proposals),
        scrape_complete=True,
        parent_account_id="PARENT",
        generated_at="2026-01-01T00:00:00+00:00",
    )


def _approve(store: ProposalStore, proposal: Proposal, status: str = STATUS_APPROVED):
    run_id = store.save_run(_batch(proposal))
    stored = store.list_proposals(run_id=run_id)[0]
    if status != STATUS_PENDING:
        store.set_status(stored.id, status)
    return store.get_proposal(stored.id)


def _approve_many(store: ProposalStore, *proposals: Proposal, status: str = STATUS_APPROVED):
    run_id = store.save_run(_batch(*proposals))
    stored = store.list_proposals(run_id=run_id)
    # list_proposals orders by id DESC; reverse for insertion order
    stored = list(reversed(stored))
    if status != STATUS_PENDING:
        for item in stored:
            store.set_status(item.id, status)
    return [store.get_proposal(item.id) for item in stored]


def _account(account_id: str, **extra) -> dict[str, Any]:
    base = {
        fields.ACCOUNT_ID: account_id,
        fields.NAME: "Bellhaven of Tiffin",
        fields.PARENT_ID: "PARENT",
        fields.STREET: "100 Main Street",
        fields.CITY: "Tiffin",
        fields.STATE: "OH",
        fields.ZIP: "44883",
        fields.CARE_TYPE: "Assisted Living",
        fields.PHONE: "419-555-0100",
        fields.STATUS: "Active",
        fields.LIFETIME_REVENUE: 0,
        fields.OUTSTANDING_AR: 0,
        fields.CHOW_CURRENT_ACCOUNT: "",
        fields.CREATED_BY_CANDIDATE: False,
    }
    base.update(extra)
    return base


def _parent() -> dict[str, Any]:
    return _account("PARENT", **{fields.PARENT_ID: "", fields.NAME: "Bellhaven Senior Living"})


def _wrong_parent() -> dict[str, Any]:
    """Former parent for CHOW fixtures; must not match the new-account template identity."""
    return _account(
        "WRONG",
        **{
            fields.PARENT_ID: "",
            fields.NAME: "Wrong Parent Co",
            fields.STREET: "1 Other Road",
            fields.CITY: "Columbus",
            fields.STATE: "OH",
            fields.ZIP: "43215",
            fields.PHONE: "614-555-0000",
        },
    )


def _run(settings, store, session, proposal, *, execute=False, dry_run=True):
    chosen = replace(settings, dry_run=dry_run)
    return run_apply(
        settings=chosen,
        store=store,
        session=session,
        run_id=proposal.run_id,
        execute=execute,
    )


def test_pending_and_rejected_do_not_write(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"C1": _account("C1"), "PARENT": _parent()})
    pending = Proposal(
        action_type=ACTION_UPDATE_FIELDS,
        account_id="C1",
        facility_url="https://example.test/a",
        current_values={fields.PHONE: "419-555-0100"},
        proposed_values={fields.PHONE: "419-555-9999"},
        evidence={},
        confidence="high",
    )
    stored = _approve(store, pending, STATUS_PENDING)
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.applied == 0
    assert session.patches == []
    assert session.posts == []

    rejected = _approve(store, pending, STATUS_REJECTED)
    report = _run(settings, store, session, rejected, execute=True, dry_run=False)
    assert report.applied == 0
    assert session.patches == []


def test_approved_review_only_and_unknown_action_do_not_write(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"PARENT": _parent()})
    review = _approve(
        store,
        Proposal(
            action_type=ACTION_REVIEW_AMBIGUOUS,
            account_id=None,
            facility_url="https://example.test/a",
            current_values={},
            proposed_values={},
            evidence={},
            confidence="ambiguous",
        ),
    )
    report = _run(settings, store, session, review, execute=True, dry_run=False)
    assert report.skipped_review == 1
    assert session.posts == []
    assert session.patches == []

    unknown = _approve(
        store,
        Proposal(
            action_type="merge_accounts",
            account_id="C1",
            facility_url=None,
            current_values={},
            proposed_values={"account_id": "C1"},
            evidence={},
            confidence="high",
        ),
    )
    report = _run(settings, store, session, unknown, execute=True, dry_run=False)
    assert report.blocked == 1
    assert report.applied == 0
    assert session.posts == []
    assert session.patches == []


def test_dry_run_plans_without_writes_and_execute_refuses_while_dry(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"C1": _account("C1"), "PARENT": _parent()})
    stored = _approve(
        store,
        Proposal(
            action_type=ACTION_UPDATE_FIELDS,
            account_id="C1",
            facility_url="https://example.test/a",
            current_values={fields.PHONE: "419-555-0100"},
            proposed_values={fields.PHONE: "419-555-9999"},
            evidence={},
            confidence="high",
        ),
    )
    report = _run(settings, store, session, stored, execute=False, dry_run=True)
    assert report.mode == MODE_DRY_RUN
    assert report.planned_patches == 1
    assert report.planned_posts == 0
    assert session.posts == []
    assert session.patches == []
    assert store.successful_attempt(stored.id) is None
    assert store.list_attempts(stored.id)[0].state == STATE_PLANNED
    assert store.get_proposal(stored.id).status == STATUS_APPROVED

    with pytest.raises(ExecuteWhileDryRunError):
        _run(settings, store, session, stored, execute=True, dry_run=True)
    assert session.patches == []

    code = apply_main(["--execute", "--db", str(store.db_path), "--run-id", str(stored.run_id)])
    assert code == 2
    assert session.patches == []


def test_update_fields_patches_only_allowed_fields(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"C1": _account("C1"), "PARENT": _parent()})
    stored = _approve(
        store,
        Proposal(
            action_type=ACTION_UPDATE_FIELDS,
            account_id="C1",
            facility_url="https://example.test/a",
            current_values={fields.PHONE: "419-555-0100"},
            proposed_values={fields.PHONE: "419-555-9999", fields.CITY: "Tiffin"},
            evidence={},
            confidence="high",
        ),
    )
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.applied == 1
    assert session.posts == []
    assert session.patches == [("C1", {fields.PHONE: "419-555-9999", fields.CITY: "Tiffin"})]
    assert store.successful_attempt(stored.id).state == STATE_APPLIED
    assert store.get_proposal(stored.id).status == STATUS_APPROVED

    again = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert again.skipped_applied == 1
    assert len(session.patches) == 1


def test_update_fields_rejects_unsafe_keys_and_drift(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"C1": _account("C1"), "PARENT": _parent()})
    unsafe = _approve(
        store,
        Proposal(
            action_type=ACTION_UPDATE_FIELDS,
            account_id="C1",
            facility_url="https://example.test/a",
            current_values={fields.STATUS: "Active"},
            proposed_values={fields.STATUS: "Inactive"},
            evidence={},
            confidence="high",
        ),
    )
    report = _run(settings, store, session, unsafe, execute=True, dry_run=False)
    assert report.blocked == 1
    assert session.patches == []

    drifted = _approve(
        store,
        Proposal(
            action_type=ACTION_UPDATE_FIELDS,
            account_id="C1",
            facility_url="https://example.test/a",
            current_values={fields.PHONE: "419-555-0100"},
            proposed_values={fields.PHONE: "419-555-9999"},
            evidence={},
            confidence="high",
        ),
    )
    session.accounts["C1"][fields.PHONE] = "419-555-0000"
    report = _run(settings, store, session, drifted, execute=True, dry_run=False)
    assert report.blocked == 1
    assert session.patches == []
    assert "phone" in store.list_attempts(drifted.id)[-1].error_text


def test_reparent_patch_is_exactly_parent_id(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"C1": _account("C1", **{fields.PARENT_ID: "OLD"}), "PARENT": _parent(), "OLD": _account("OLD")})
    stored = _approve(
        store,
        Proposal(
            action_type=ACTION_REPARENT,
            account_id="C1",
            facility_url="https://example.test/a",
            current_values={fields.PARENT_ID: "OLD"},
            proposed_values={fields.PARENT_ID: "PARENT"},
            evidence={},
            confidence="high",
        ),
    )
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.applied == 1
    assert session.patches == [("C1", {fields.PARENT_ID: "PARENT"})]
    assert set(session.patches[0][1]) == {fields.PARENT_ID}

    session.accounts["C1"][fields.PARENT_ID] = "OTHER"
    moved = _approve(
        store,
        Proposal(
            action_type=ACTION_REPARENT,
            account_id="C1",
            facility_url="https://example.test/a",
            current_values={fields.PARENT_ID: "OLD"},
            proposed_values={fields.PARENT_ID: "PARENT"},
            evidence={},
            confidence="high",
        ),
    )
    report = _run(settings, store, session, moved, execute=True, dry_run=False)
    assert report.blocked == 1
    assert len(session.patches) == 1


def test_create_posts_safe_fields_and_does_not_repeat(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"PARENT": _parent()})
    proposed = {
        fields.NAME: "Brand New Place",
        fields.STREET: "9 Pine St",
        fields.CITY: "Tiffin",
        fields.STATE: "OH",
        fields.ZIP: "44880",
        fields.CARE_TYPE: "Memory Care",
        fields.PHONE: "419-555-2222",
        fields.PARENT_ID: "PARENT",
        fields.STATUS: "Active",
    }
    stored = _approve(
        store,
        Proposal(
            action_type=ACTION_CREATE_ACCOUNT,
            account_id=None,
            facility_url="https://example.test/new",
            current_values={},
            proposed_values=proposed,
            evidence={},
            confidence="medium",
        ),
    )
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.applied == 1
    assert len(session.posts) == 1
    posted = session.posts[0]
    assert posted[fields.CREATED_BY_CANDIDATE] is True
    assert posted[fields.PARENT_ID] == "PARENT"
    assert posted[fields.CARE_TYPE] == "Memory Care"
    assert fields.LIFETIME_REVENUE not in posted
    assert fields.OUTSTANDING_AR not in posted
    assert fields.CHOW_CURRENT_ACCOUNT not in posted
    assert fields.NOTE not in posted
    assert fields.ACCOUNT_ID not in posted
    created_id = store.successful_attempt(stored.id).created_account_id
    assert created_id.startswith("NEW")

    again = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert again.skipped_applied == 1
    assert len(session.posts) == 1


def test_create_recovers_one_tool_owned_account_and_stops_on_two(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    proposed = {
        fields.NAME: "Brand New Place",
        fields.STREET: "9 Pine St",
        fields.CITY: "Tiffin",
        fields.STATE: "OH",
        fields.ZIP: "44880",
        fields.PARENT_ID: "PARENT",
        fields.STATUS: "Active",
    }
    owned = _account(
        "OWN1",
        **{
            fields.NAME: "Brand New Place",
            fields.STREET: "9 Pine St",
            fields.CITY: "Tiffin",
            fields.STATE: "OH",
            fields.ZIP: "44880",
            fields.PARENT_ID: "PARENT",
            fields.CREATED_BY_CANDIDATE: True,
        },
    )
    lookalike = _account(
        "OLD",
        **{
            fields.NAME: "Brand New Place",
            fields.STREET: "9 Pine St",
            fields.CITY: "Tiffin",
            fields.STATE: "OH",
            fields.ZIP: "44880",
            fields.PARENT_ID: "OTHER-PARENT",
            fields.CREATED_BY_CANDIDATE: False,
        },
    )
    session = CrmFake(
        {"PARENT": _parent(), "OWN1": owned, "OLD": lookalike, "OTHER-PARENT": _account("OTHER-PARENT")}
    )
    stored = _approve(
        store,
        Proposal(
            action_type=ACTION_CREATE_ACCOUNT,
            account_id=None,
            facility_url="https://example.test/new",
            current_values={},
            proposed_values=proposed,
            evidence={},
            confidence="medium",
        ),
    )
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.applied == 1
    assert session.posts == []
    assert store.successful_attempt(stored.id).created_account_id == "OWN1"

    second = _approve(
        store,
        Proposal(
            action_type=ACTION_CREATE_ACCOUNT,
            account_id=None,
            facility_url="https://example.test/new-2",
            current_values={},
            proposed_values=proposed,
            evidence={},
            confidence="medium",
        ),
    )
    session.accounts["OWN2"] = _account(
        "OWN2",
        **{
            fields.NAME: "Brand New Place",
            fields.STREET: "9 Pine St",
            fields.CITY: "Tiffin",
            fields.STATE: "OH",
            fields.ZIP: "44880",
            fields.PARENT_ID: "PARENT",
            fields.CREATED_BY_CANDIDATE: True,
        },
    )
    report = _run(settings, store, session, second, execute=True, dry_run=False)
    assert report.blocked == 1
    assert session.posts == []


def test_chow_posts_then_patches_only_the_link(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    old = _account(
        "OLD1",
        **{
            fields.PARENT_ID: "WRONG",
            fields.LIFETIME_REVENUE: 9000,
            fields.OUTSTANDING_AR: 300,
        },
    )
    session = CrmFake({"OLD1": old, "PARENT": _parent(), "WRONG": _wrong_parent()})
    stored = _approve(
        store,
        Proposal(
            action_type=ACTION_CHOW,
            account_id="OLD1",
            facility_url="https://example.test/a",
            current_values={
                fields.PARENT_ID: "WRONG",
                fields.NAME: old[fields.NAME],
                fields.STREET: old[fields.STREET],
                fields.CITY: old[fields.CITY],
                fields.STATE: old[fields.STATE],
                fields.ZIP: old[fields.ZIP],
                fields.LIFETIME_REVENUE: 9000,
                fields.OUTSTANDING_AR: 300,
            },
            proposed_values={
                "new_account_parent_id": "PARENT",
                "new_account_template": {
                    fields.NAME: "Bellhaven of Tiffin",
                    fields.STREET: "100 Main Street",
                    fields.CITY: "Tiffin",
                    fields.STATE: "OH",
                    fields.ZIP: "44883",
                    fields.PHONE: "419-555-0100",
                    fields.CARE_TYPE: "Memory Care",
                },
                "old_account_patch": {fields.CHOW_CURRENT_ACCOUNT: "<new_account_id>"},
                "old_account_unchanged": [fields.PARENT_ID, fields.STATUS, fields.NAME],
            },
            evidence={},
            confidence="high",
        ),
    )
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.applied == 1
    assert len(session.posts) == 1
    assert session.posts[0][fields.PARENT_ID] == "PARENT"
    assert session.posts[0][fields.CREATED_BY_CANDIDATE] is True
    new_id = store.successful_attempt(stored.id).created_account_id
    assert session.patches == [("OLD1", {fields.CHOW_CURRENT_ACCOUNT: new_id})]
    assert list(session.patches[0][1]) == [fields.CHOW_CURRENT_ACCOUNT]
    assert session.accounts["OLD1"][fields.PARENT_ID] == "WRONG"
    assert session.accounts["OLD1"][fields.NAME] == old[fields.NAME]


def test_chow_resume_does_not_post_twice_and_conflict_blocks(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    old = _account("OLD1", **{fields.PARENT_ID: "WRONG", fields.LIFETIME_REVENUE: 9000, fields.OUTSTANDING_AR: 300})
    session = CrmFake({"OLD1": old, "PARENT": _parent()})
    session.patch_failures = 1
    proposal = Proposal(
        action_type=ACTION_CHOW,
        account_id="OLD1",
        facility_url="https://example.test/a",
        current_values={
            fields.PARENT_ID: "WRONG",
            fields.LIFETIME_REVENUE: 9000,
            fields.OUTSTANDING_AR: 300,
        },
        proposed_values={
            "new_account_parent_id": "PARENT",
            "new_account_template": {fields.NAME: "Bellhaven of Tiffin", fields.STREET: "100 Main Street"},
            "old_account_patch": {fields.CHOW_CURRENT_ACCOUNT: "<new_account_id>"},
        },
        evidence={},
        confidence="high",
    )
    stored = _approve(store, proposal)
    first = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert first.failed == 1
    assert len(session.posts) == 1
    new_id = store.created_account_id_for_proposal(stored.id)
    assert new_id
    assert FAKE_TOKEN not in (store.list_attempts(stored.id)[-1].error_text or "")
    assert store.successful_attempt(stored.id) is None

    second = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert second.applied == 1
    assert len(session.posts) == 1
    assert session.patches[-1] == ( "OLD1", {fields.CHOW_CURRENT_ACCOUNT: new_id})

    session.accounts["OLD1"][fields.CHOW_CURRENT_ACCOUNT] = "SOMEONE-ELSE"
    store2_proposal = _approve(store, proposal)
    store.record_attempt(
        proposal_id=store2_proposal.id,
        run_id=store2_proposal.run_id,
        action_type=ACTION_CHOW,
        mode=MODE_EXECUTE,
        state=STATE_FAILED,
        created_account_id="NEW-RECORDED",
        error_text="previous link failed",
    )
    conflict = _run(settings, store, session, store2_proposal, execute=True, dry_run=False)
    assert conflict.blocked == 1
    assert len(session.posts) == 1
    assert all(body.get(fields.CHOW_CURRENT_ACCOUNT) != "NEW-RECORDED" for _, body in session.patches)


def test_chow_already_linked_is_complete_and_financial_drift_blocks(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    old = _account(
        "OLD1",
        **{fields.PARENT_ID: "WRONG", fields.LIFETIME_REVENUE: 9000, fields.OUTSTANDING_AR: 300, fields.CHOW_CURRENT_ACCOUNT: "NEW1"},
    )
    session = CrmFake({"OLD1": old, "PARENT": _parent(), "NEW1": _account("NEW1", **{fields.CREATED_BY_CANDIDATE: True})})
    stored = _approve(
        store,
        Proposal(
            action_type=ACTION_CHOW,
            account_id="OLD1",
            facility_url="https://example.test/a",
            current_values={fields.PARENT_ID: "WRONG", fields.OUTSTANDING_AR: 300},
            proposed_values={
                "new_account_parent_id": "PARENT",
                "new_account_template": {fields.NAME: "Bellhaven of Tiffin"},
                "old_account_patch": {fields.CHOW_CURRENT_ACCOUNT: "<new_account_id>"},
            },
            evidence={},
            confidence="high",
        ),
    )
    store.record_attempt(
        proposal_id=stored.id,
        run_id=stored.run_id,
        action_type=ACTION_CHOW,
        mode=MODE_EXECUTE,
        state=STATE_FAILED,
        created_account_id="NEW1",
        error_text="link uncertain",
    )
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.applied == 1
    assert session.posts == []
    assert session.patches == []

    drifted = _approve(
        store,
        Proposal(
            action_type=ACTION_CHOW,
            account_id="OLD1",
            facility_url="https://example.test/a",
            current_values={fields.PARENT_ID: "WRONG", fields.OUTSTANDING_AR: 300, fields.LIFETIME_REVENUE: 9000},
            proposed_values={
                "new_account_parent_id": "PARENT",
                "new_account_template": {fields.NAME: "Bellhaven of Tiffin"},
                "old_account_patch": {fields.CHOW_CURRENT_ACCOUNT: "<new_account_id>"},
            },
            evidence={},
            confidence="high",
        ),
    )
    session.accounts["OLD1"][fields.CHOW_CURRENT_ACCOUNT] = ""
    session.accounts["OLD1"][fields.OUTSTANDING_AR] = 1
    report = _run(settings, store, session, drifted, execute=True, dry_run=False)
    assert report.blocked == 1
    assert session.posts == []


def test_post_is_not_retried_and_error_is_redacted(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"PARENT": _parent()})
    session.post_failures = 1
    stored = _approve(
        store,
        Proposal(
            action_type=ACTION_CREATE_ACCOUNT,
            account_id=None,
            facility_url="https://example.test/new",
            current_values={},
            proposed_values={
                fields.NAME: "Brand New Place",
                fields.PARENT_ID: "PARENT",
                fields.STATUS: "Active",
            },
            evidence={},
            confidence="medium",
        ),
    )
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.failed == 1
    assert len(session.posts) == 1
    error = store.list_attempts(stored.id)[-1].error_text or ""
    assert FAKE_TOKEN not in error
    assert "Not retrying" in error
    assert store.list_attempts(stored.id)[-1].state == STATE_UNCERTAIN


def _chow_proposal(*, account_id: str = "OLD1", template: dict[str, Any] | None = None) -> Proposal:
    return Proposal(
        action_type=ACTION_CHOW,
        account_id=account_id,
        facility_url="https://example.test/a",
        current_values={
            fields.PARENT_ID: "WRONG",
            fields.NAME: "Bellhaven of Tiffin",
            fields.STREET: "100 Main Street",
            fields.CITY: "Tiffin",
            fields.STATE: "OH",
            fields.ZIP: "44883",
            fields.LIFETIME_REVENUE: 9000,
            fields.OUTSTANDING_AR: 300,
        },
        proposed_values={
            "new_account_parent_id": "PARENT",
            "new_account_template": template
            or {
                fields.NAME: "Bellhaven of Tiffin",
                fields.STREET: "100 Main Street",
                fields.CITY: "Tiffin",
                fields.STATE: "OH",
                fields.ZIP: "44883",
                fields.PHONE: "419-555-0100",
                fields.CARE_TYPE: "Memory Care",
            },
            "old_account_patch": {fields.CHOW_CURRENT_ACCOUNT: "<new_account_id>"},
        },
        evidence={},
        confidence="high",
    )


def test_chow_recovers_lost_post_response_without_second_post(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    old = _account(
        "OLD1",
        **{fields.PARENT_ID: "WRONG", fields.LIFETIME_REVENUE: 9000, fields.OUTSTANDING_AR: 300},
    )
    session = CrmFake({"OLD1": old, "PARENT": _parent(), "WRONG": _wrong_parent()})
    session.lose_post_response = 1
    stored = _approve(store, _chow_proposal())

    first = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert first.failed == 1
    assert len(session.posts) == 1
    assert session.patches == []
    assert store.created_account_id_for_proposal(stored.id) is None
    assert store.has_uncertain_attempt(stored.id)
    assert store.list_attempts(stored.id)[-1].state == STATE_UNCERTAIN
    recovered_id = next(aid for aid in session.accounts if aid.startswith("NEW"))
    assert session.accounts[recovered_id][fields.CREATED_BY_CANDIDATE] is True

    second = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert second.applied == 1
    assert len(session.posts) == 1
    assert session.patches == [("OLD1", {fields.CHOW_CURRENT_ACCOUNT: recovered_id})]
    assert store.successful_attempt(stored.id).created_account_id == recovered_id
    assert session.accounts["OLD1"][fields.CHOW_CURRENT_ACCOUNT] == recovered_id


def test_chow_uncertain_with_no_visible_account_refuses_second_post(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    old = _account(
        "OLD1",
        **{fields.PARENT_ID: "WRONG", fields.LIFETIME_REVENUE: 9000, fields.OUTSTANDING_AR: 300},
    )
    session = CrmFake({"OLD1": old, "PARENT": _parent(), "WRONG": _wrong_parent()})
    session.post_failures = 1
    stored = _approve(store, _chow_proposal())

    first = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert first.failed == 1
    assert len(session.posts) == 1
    assert store.has_uncertain_attempt(stored.id)
    assert store.created_account_id_for_proposal(stored.id) is None

    second = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert second.blocked == 1
    assert len(session.posts) == 1
    assert session.patches == []
    assert store.successful_attempt(stored.id) is None
    error = store.list_attempts(stored.id)[-1].error_text or ""
    assert "Refusing a second POST" in error
    assert store.list_attempts(stored.id)[-1].state == STATE_BLOCKED


def test_chow_multiple_recovery_matches_blocks_without_write(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    template = {
        fields.NAME: "Bellhaven of Tiffin",
        fields.STREET: "100 Main Street",
        fields.CITY: "Tiffin",
        fields.STATE: "OH",
        fields.ZIP: "44883",
    }
    twin_a = _account(
        "OWN-A",
        **{**template, fields.PARENT_ID: "PARENT", fields.CREATED_BY_CANDIDATE: True},
    )
    twin_b = _account(
        "OWN-B",
        **{**template, fields.PARENT_ID: "PARENT", fields.CREATED_BY_CANDIDATE: True},
    )
    old = _account(
        "OLD1",
        **{fields.PARENT_ID: "WRONG", fields.LIFETIME_REVENUE: 9000, fields.OUTSTANDING_AR: 300},
    )
    session = CrmFake(
        {
            "OLD1": old,
            "PARENT": _parent(),
            "WRONG": _wrong_parent(),
            "OWN-A": twin_a,
            "OWN-B": twin_b,
        }
    )
    stored = _approve(store, _chow_proposal(template=template))
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.blocked == 1
    assert session.posts == []
    assert session.patches == []
    assert "human review" in (store.list_attempts(stored.id)[-1].error_text or "")


def test_recovery_scan_crm_error_stays_on_current_proposal(settings, tmp_path):
    from bellhaven_sync import crm_client

    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"C1": _account("C1"), "PARENT": _parent()})
    session.list_error = crm_client.CrmError(f"list boom {FAKE_TOKEN}", status_code=500)
    create_proposal = Proposal(
        action_type=ACTION_CREATE_ACCOUNT,
        account_id=None,
        facility_url="https://example.test/new",
        current_values={},
        proposed_values={
            fields.NAME: "Brand New Place",
            fields.STREET: "9 Pine St",
            fields.CITY: "Tiffin",
            fields.STATE: "OH",
            fields.ZIP: "44880",
            fields.PARENT_ID: "PARENT",
            fields.STATUS: "Active",
        },
        evidence={},
        confidence="medium",
    )
    update_proposal = Proposal(
        action_type=ACTION_UPDATE_FIELDS,
        account_id="C1",
        facility_url="https://example.test/a",
        current_values={fields.PHONE: "419-555-0100"},
        proposed_values={fields.PHONE: "419-555-9999"},
        evidence={},
        confidence="high",
    )
    update_stored, create_stored = _approve_many(store, update_proposal, create_proposal)
    report = run_apply(
        settings=replace(settings, dry_run=False),
        store=store,
        session=session,
        run_id=create_stored.run_id,
        execute=True,
    )
    assert report.blocked == 1
    assert report.applied == 1
    assert session.posts == []
    create_attempts = store.list_attempts(create_stored.id)
    assert create_attempts
    assert create_attempts[-1].state == STATE_BLOCKED
    assert create_attempts[-1].state != STATE_IN_PROGRESS
    error = create_attempts[-1].error_text or ""
    assert FAKE_TOKEN not in error
    assert "could not scan" in error
    assert store.successful_attempt(update_stored.id) is not None
    assert session.patches == [("C1", {fields.PHONE: "419-555-9999"})]


def _create_proposal(proposed: dict[str, Any] | None = None) -> Proposal:
    body = proposed or {
        fields.NAME: "Brand New Place",
        fields.STREET: "9 Pine St",
        fields.CITY: "Tiffin",
        fields.STATE: "OH",
        fields.ZIP: "44880",
        fields.PARENT_ID: "PARENT",
        fields.STATUS: "Active",
    }
    return Proposal(
        action_type=ACTION_CREATE_ACCOUNT,
        account_id=None,
        facility_url="https://example.test/new",
        current_values={},
        proposed_values=body,
        evidence={},
        confidence="medium",
    )


def test_create_uncertain_with_no_visible_account_refuses_second_post(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"PARENT": _parent()})
    session.post_failures = 1
    stored = _approve(store, _create_proposal())

    first = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert first.failed == 1
    assert len(session.posts) == 1
    assert store.has_uncertain_attempt(stored.id)
    assert store.list_attempts(stored.id)[-1].state == STATE_UNCERTAIN
    assert store.created_account_id_for_proposal(stored.id) is None

    second = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert second.blocked == 1
    assert len(session.posts) == 1
    assert store.successful_attempt(stored.id) is None
    error = store.list_attempts(stored.id)[-1].error_text or ""
    assert "Refusing a second POST" in error
    assert store.list_attempts(stored.id)[-1].state == STATE_BLOCKED


def test_create_recovers_lost_post_response_without_second_post(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"PARENT": _parent()})
    session.lose_post_response = 1
    stored = _approve(store, _create_proposal())

    first = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert first.failed == 1
    assert len(session.posts) == 1
    assert store.has_uncertain_attempt(stored.id)
    assert store.created_account_id_for_proposal(stored.id) is None
    recovered_id = next(aid for aid in session.accounts if aid.startswith("NEW"))
    assert session.accounts[recovered_id][fields.CREATED_BY_CANDIDATE] is True

    second = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert second.applied == 1
    assert len(session.posts) == 1
    assert store.successful_attempt(stored.id).created_account_id == recovered_id


def test_non_tool_match_blocks_ordinary_create(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    proposed = {
        fields.NAME: "Brand New Place",
        fields.STREET: "9 Pine St",
        fields.CITY: "Tiffin",
        fields.STATE: "OH",
        fields.ZIP: "44880",
        fields.PARENT_ID: "PARENT",
        fields.STATUS: "Active",
    }
    outsider = _account(
        "OUT1",
        **{
            fields.NAME: "Brand New Place",
            fields.STREET: "9 Pine St",
            fields.CITY: "Tiffin",
            fields.STATE: "OH",
            fields.ZIP: "44880",
            fields.PARENT_ID: "PARENT",
            fields.CREATED_BY_CANDIDATE: False,
        },
    )
    session = CrmFake({"PARENT": _parent(), "OUT1": outsider})
    stored = _approve(store, _create_proposal(proposed))
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.blocked == 1
    assert session.posts == []
    assert store.successful_attempt(stored.id) is None
    assert store.created_account_id_for_proposal(stored.id) is None
    error = store.list_attempts(stored.id)[-1].error_text or ""
    assert "not created by this tool" in error


def test_non_tool_match_blocks_chow_create(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    template = {
        fields.NAME: "Bellhaven of Tiffin",
        fields.STREET: "100 Main Street",
        fields.CITY: "Tiffin",
        fields.STATE: "OH",
        fields.ZIP: "44883",
    }
    outsider = _account(
        "OUT1",
        **{**template, fields.PARENT_ID: "PARENT", fields.CREATED_BY_CANDIDATE: False},
    )
    old = _account(
        "OLD1",
        **{fields.PARENT_ID: "WRONG", fields.LIFETIME_REVENUE: 9000, fields.OUTSTANDING_AR: 300},
    )
    session = CrmFake(
        {"OLD1": old, "PARENT": _parent(), "WRONG": _wrong_parent(), "OUT1": outsider}
    )
    stored = _approve(store, _chow_proposal(template=template))
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.blocked == 1
    assert session.posts == []
    assert session.patches == []
    assert store.created_account_id_for_proposal(stored.id) is None
    error = store.list_attempts(stored.id)[-1].error_text or ""
    assert "not created by this tool" in error


def test_mixed_tool_and_non_tool_matches_block_create(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    proposed = {
        fields.NAME: "Brand New Place",
        fields.STREET: "9 Pine St",
        fields.CITY: "Tiffin",
        fields.STATE: "OH",
        fields.ZIP: "44880",
        fields.PARENT_ID: "PARENT",
        fields.STATUS: "Active",
    }
    tool = _account(
        "OWN1",
        **{
            fields.NAME: "Brand New Place",
            fields.STREET: "9 Pine St",
            fields.CITY: "Tiffin",
            fields.STATE: "OH",
            fields.ZIP: "44880",
            fields.PARENT_ID: "PARENT",
            fields.CREATED_BY_CANDIDATE: True,
        },
    )
    outsider = _account(
        "OUT1",
        **{
            fields.NAME: "Brand New Place",
            fields.STREET: "9 Pine St",
            fields.CITY: "Tiffin",
            fields.STATE: "OH",
            fields.ZIP: "44880",
            fields.PARENT_ID: "PARENT",
            fields.CREATED_BY_CANDIDATE: False,
        },
    )
    session = CrmFake({"PARENT": _parent(), "OWN1": tool, "OUT1": outsider})
    stored = _approve(store, _create_proposal(proposed))
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.blocked == 1
    assert session.posts == []
    assert store.successful_attempt(stored.id) is None
    assert store.created_account_id_for_proposal(stored.id) is None
    error = store.list_attempts(stored.id)[-1].error_text or ""
    assert "not created by this tool" in error


def test_claim_execute_attempt_is_atomic_across_store_handles(tmp_path):
    db = tmp_path / "db.sqlite"
    store_a = ProposalStore(db)
    store_b = ProposalStore(db)
    stored = _approve(store_a, _create_proposal())

    first = store_a.claim_execute_attempt(
        proposal_id=stored.id,
        run_id=stored.run_id,
        action_type=ACTION_CREATE_ACCOUNT,
    )
    second = store_b.claim_execute_attempt(
        proposal_id=stored.id,
        run_id=stored.run_id,
        action_type=ACTION_CREATE_ACCOUNT,
    )
    assert first.status == CLAIM_CLAIMED
    assert first.attempt is not None
    assert first.attempt.state == STATE_IN_PROGRESS
    assert second.status == CLAIM_IN_PROGRESS
    assert second.attempt is not None
    assert second.attempt.id == first.attempt.id

    attempts = store_a.list_attempts(stored.id)
    in_progress = [item for item in attempts if item.state == STATE_IN_PROGRESS]
    assert len(in_progress) == 1

    again = store_b.claim_execute_attempt(
        proposal_id=stored.id,
        run_id=stored.run_id,
        action_type=ACTION_CREATE_ACCOUNT,
    )
    assert again.status == CLAIM_IN_PROGRESS
    assert len([item for item in store_a.list_attempts(stored.id) if item.state == STATE_IN_PROGRESS]) == 1


def test_orphaned_in_progress_attempt_blocks_execute_without_write(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"PARENT": _parent()})
    stored = _approve(store, _create_proposal())
    orphan = store.record_attempt(
        proposal_id=stored.id,
        run_id=stored.run_id,
        action_type=ACTION_CREATE_ACCOUNT,
        mode=MODE_EXECUTE,
        state=STATE_IN_PROGRESS,
    )
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.blocked == 1
    assert report.applied == 0
    assert session.posts == []
    assert session.patches == []
    attempts = store.list_attempts(stored.id)
    assert len(attempts) == 1
    assert attempts[0].id == orphan.id
    assert attempts[0].state == STATE_IN_PROGRESS
    assert any("in_progress" in note for note in report.notes)
    assert any("interrupted" in note for note in report.notes)


def test_blank_phone_still_blocks_matching_non_tool_create(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    proposed = {
        fields.NAME: "Brand New Place",
        fields.STREET: "9 Pine St",
        fields.CITY: "Tiffin",
        fields.STATE: "OH",
        fields.ZIP: "44880",
        fields.PHONE: "",
        fields.PARENT_ID: "PARENT",
        fields.STATUS: "Active",
    }
    outsider = _account(
        "OUT1",
        **{
            fields.NAME: "Brand New Place",
            fields.STREET: "9 Pine St",
            fields.CITY: "Tiffin",
            fields.STATE: "OH",
            fields.ZIP: "44880",
            fields.PHONE: "419-555-9999",
            fields.PARENT_ID: "PARENT",
            fields.CREATED_BY_CANDIDATE: False,
        },
    )
    session = CrmFake({"PARENT": _parent(), "OUT1": outsider})
    stored = _approve(store, _create_proposal(proposed))
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.blocked == 1
    assert session.posts == []
    assert store.created_account_id_for_proposal(stored.id) is None
    assert "not created by this tool" in (store.list_attempts(stored.id)[-1].error_text or "")


def test_tool_recovery_ignores_phone_difference(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    proposed = {
        fields.NAME: "Brand New Place",
        fields.STREET: "9 Pine St",
        fields.CITY: "Tiffin",
        fields.STATE: "OH",
        fields.ZIP: "44880",
        fields.PHONE: "",
        fields.PARENT_ID: "PARENT",
        fields.STATUS: "Active",
    }
    owned = _account(
        "OWN1",
        **{
            fields.NAME: "Brand New Place",
            fields.STREET: "9 Pine St",
            fields.CITY: "Tiffin",
            fields.STATE: "OH",
            fields.ZIP: "44880",
            fields.PHONE: "419-555-7777",
            fields.PARENT_ID: "PARENT",
            fields.CREATED_BY_CANDIDATE: True,
        },
    )
    session = CrmFake({"PARENT": _parent(), "OWN1": owned})
    stored = _approve(store, _create_proposal(proposed))
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.applied == 1
    assert session.posts == []
    assert store.successful_attempt(stored.id).created_account_id == "OWN1"


def test_second_apply_while_claimed_issues_zero_writes(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"PARENT": _parent()})
    stored = _approve(store, _create_proposal())
    claimed = store.claim_execute_attempt(
        proposal_id=stored.id,
        run_id=stored.run_id,
        action_type=ACTION_CREATE_ACCOUNT,
    )
    assert claimed.status == CLAIM_CLAIMED
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.blocked == 1
    assert session.posts == []
    assert session.patches == []
    assert len([a for a in store.list_attempts(stored.id) if a.state == STATE_IN_PROGRESS]) == 1


def test_claim_after_applied_reports_already_applied(tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    stored = _approve(store, _create_proposal())
    store.record_attempt(
        proposal_id=stored.id,
        run_id=stored.run_id,
        action_type=ACTION_CREATE_ACCOUNT,
        mode=MODE_EXECUTE,
        state=STATE_APPLIED,
        created_account_id="DONE1",
    )
    result = store.claim_execute_attempt(
        proposal_id=stored.id,
        run_id=stored.run_id,
        action_type=ACTION_CREATE_ACCOUNT,
    )
    assert result.status == CLAIM_ALREADY_APPLIED
    assert len([a for a in store.list_attempts(stored.id) if a.state == STATE_IN_PROGRESS]) == 0


def test_blank_proposed_city_zip_still_match_existing_non_tool_account(settings, tmp_path):
    from bellhaven_sync import apply as apply_mod

    store = ProposalStore(tmp_path / "db.sqlite")
    proposed = {
        fields.NAME: "Brand New Place",
        fields.STREET: "9 Pine St",
        fields.CITY: "  ",
        fields.STATE: "OH",
        fields.ZIP: "",
        fields.PARENT_ID: "PARENT",
        fields.STATUS: "Active",
    }
    outsider = _account(
        "OUT1",
        **{
            fields.NAME: "Brand New Place",
            fields.STREET: "9 Pine St",
            fields.CITY: "Tiffin",
            fields.STATE: "OH",
            fields.ZIP: "44880",
            fields.PARENT_ID: "PARENT",
            fields.CREATED_BY_CANDIDATE: False,
        },
    )
    session = CrmFake({"PARENT": _parent(), "OUT1": outsider})
    stored = _approve(store, _create_proposal(proposed))
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.blocked == 1
    assert session.posts == []
    assert store.created_account_id_for_proposal(stored.id) is None
    body = {**proposed, fields.CREATED_BY_CANDIDATE: True}
    assert apply_mod._same_identity(outsider, body) is True
    assert "not created by this tool" in (store.list_attempts(stored.id)[-1].error_text or "")


def test_blank_proposed_city_zip_recovers_tool_created_account(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    proposed = {
        fields.NAME: "Brand New Place",
        fields.STREET: "9 Pine St",
        fields.CITY: "",
        fields.STATE: "OH",
        fields.ZIP: "   ",
        fields.PARENT_ID: "PARENT",
        fields.STATUS: "Active",
    }
    owned = _account(
        "OWN1",
        **{
            fields.NAME: "Brand New Place",
            fields.STREET: "9 Pine St",
            fields.CITY: "Tiffin",
            fields.STATE: "OH",
            fields.ZIP: "44880",
            fields.PARENT_ID: "PARENT",
            fields.CREATED_BY_CANDIDATE: True,
        },
    )
    session = CrmFake({"PARENT": _parent(), "OWN1": owned})
    stored = _approve(store, _create_proposal(proposed))
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.applied == 1
    assert session.posts == []
    assert store.successful_attempt(stored.id).created_account_id == "OWN1"


def test_same_create_identity_blocks_second_proposal_from_posting(settings, tmp_path):
    from bellhaven_sync import apply as apply_mod

    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"PARENT": _parent()})
    proposed = {
        fields.NAME: "Brand New Place",
        fields.STREET: "9 Pine St",
        fields.CITY: "Tiffin",
        fields.STATE: "OH",
        fields.ZIP: "44880",
        fields.PARENT_ID: "PARENT",
        fields.STATUS: "Active",
    }
    first, second = _approve_many(store, _create_proposal(proposed), _create_proposal(proposed))
    first_claim = store.claim_execute_attempt(
        proposal_id=first.id,
        run_id=first.run_id,
        action_type=ACTION_CREATE_ACCOUNT,
    )
    assert first_claim.status == CLAIM_CLAIMED
    assert first_claim.attempt is not None
    body = {**proposed, fields.CREATED_BY_CANDIDATE: True}
    assert store.claim_create_identity(
        identity_key=apply_mod._create_identity_key(body),
        proposal_id=first.id,
        attempt_id=first_claim.attempt.id,
    )

    report = run_apply(
        settings=replace(settings, dry_run=False),
        store=store,
        session=session,
        run_id=second.run_id,
        proposal_id=second.id,
        execute=True,
    )
    assert report.blocked == 1
    assert session.posts == []
    assert session.patches == []
    error = store.list_attempts(second.id)[-1].error_text or ""
    assert "already creating an account with this identity" in error
    assert store.list_attempts(first.id)[-1].state == STATE_IN_PROGRESS


def test_uncertain_create_keeps_identity_lock_against_other_proposal(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"PARENT": _parent()})
    session.post_failures = 1
    proposed = {
        fields.NAME: "Brand New Place",
        fields.STREET: "9 Pine St",
        fields.CITY: "Tiffin",
        fields.STATE: "OH",
        fields.ZIP: "44880",
        fields.PARENT_ID: "PARENT",
        fields.STATUS: "Active",
    }
    first, second = _approve_many(store, _create_proposal(proposed), _create_proposal(proposed))

    first_report = run_apply(
        settings=replace(settings, dry_run=False),
        store=store,
        session=session,
        run_id=first.run_id,
        proposal_id=first.id,
        execute=True,
    )
    assert first_report.failed == 1
    assert store.list_attempts(first.id)[-1].state == STATE_UNCERTAIN
    assert len(session.posts) == 1

    second_report = run_apply(
        settings=replace(settings, dry_run=False),
        store=store,
        session=session,
        run_id=second.run_id,
        proposal_id=second.id,
        execute=True,
    )
    assert second_report.blocked == 1
    assert len(session.posts) == 1
    error = store.list_attempts(second.id)[-1].error_text or ""
    assert "already creating an account with this identity" in error


def test_original_proposal_recovers_after_uncertain_create(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"PARENT": _parent()})
    session.lose_post_response = 1
    proposed = {
        fields.NAME: "Brand New Place",
        fields.STREET: "9 Pine St",
        fields.CITY: "Tiffin",
        fields.STATE: "OH",
        fields.ZIP: "44880",
        fields.PARENT_ID: "PARENT",
        fields.STATUS: "Active",
    }
    stored = _approve(store, _create_proposal(proposed))

    first = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert first.failed == 1
    assert store.has_uncertain_attempt(stored.id)
    assert len(session.posts) == 1
    recovered_id = next(aid for aid in session.accounts if aid.startswith("NEW"))

    second = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert second.applied == 1
    assert len(session.posts) == 1
    assert store.successful_attempt(stored.id).created_account_id == recovered_id


def test_equivalent_street_formatting_shares_create_identity(settings, tmp_path):
    from bellhaven_sync import apply as apply_mod

    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"PARENT": _parent()})
    first_body = {
        fields.NAME: "Bellhaven of Tiffin",
        fields.STREET: "100 Main Street",
        fields.CITY: "Tiffin",
        fields.STATE: "OH",
        fields.ZIP: "44883",
        fields.PARENT_ID: "PARENT",
        fields.STATUS: "Active",
    }
    second_body = {
        fields.NAME: "Bellhaven of Tiffin",
        fields.STREET: "100 Main St.",
        fields.CITY: "Tiffin",
        fields.STATE: "oh",
        fields.ZIP: "44883-1234",
        fields.PARENT_ID: "PARENT",
        fields.STATUS: "Active",
    }
    assert apply_mod._create_identity_key({**first_body, fields.CREATED_BY_CANDIDATE: True}) == (
        apply_mod._create_identity_key({**second_body, fields.CREATED_BY_CANDIDATE: True})
    )
    owned = _account(
        "OWN1",
        **{
            fields.NAME: "Bellhaven of Tiffin",
            fields.STREET: "100 Main Street",
            fields.CITY: "Tiffin",
            fields.STATE: "OH",
            fields.ZIP: "44883",
            fields.PARENT_ID: "PARENT",
            fields.CREATED_BY_CANDIDATE: True,
        },
    )
    session.accounts["OWN1"] = owned
    stored = _approve(store, _create_proposal(second_body))
    report = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert report.applied == 1
    assert session.posts == []
    assert store.successful_attempt(stored.id).created_account_id == "OWN1"

    outsider = _account(
        "OUT1",
        **{
            fields.NAME: "Bellhaven of Tiffin",
            fields.STREET: "100 Main St",
            fields.CITY: "Tiffin",
            fields.STATE: "OH",
            fields.ZIP: "44883",
            fields.PARENT_ID: "PARENT",
            fields.CREATED_BY_CANDIDATE: False,
        },
    )
    store2 = ProposalStore(tmp_path / "db2.sqlite")
    session2 = CrmFake({"PARENT": _parent(), "OUT1": outsider})
    blocked = _approve(store2, _create_proposal(first_body))
    blocked_report = _run(settings, store2, session2, blocked, execute=True, dry_run=False)
    assert blocked_report.blocked == 1
    assert session2.posts == []


def test_claim_refuses_when_approval_revoked_before_execute(settings, tmp_path):
    from bellhaven_sync.apply import ApplyReport, _consider

    store = ProposalStore(tmp_path / "db.sqlite")
    session = CrmFake({"PARENT": _parent()})
    stored = _approve(store, _create_proposal())
    assert stored.status == STATUS_APPROVED

    # Review UI rejects after apply already loaded an approved snapshot.
    store.set_status(stored.id, STATUS_REJECTED)
    claim = store.claim_execute_attempt(
        proposal_id=stored.id,
        run_id=stored.run_id,
        action_type=ACTION_CREATE_ACCOUNT,
    )
    assert claim.status == CLAIM_NOT_APPROVED
    assert claim.attempt is None
    assert store.list_attempts(stored.id) == []

    # Stale in-memory approved proposal must still fail closed with zero writes.
    report = ApplyReport(run_id=stored.run_id, mode=MODE_EXECUTE)
    _consider(
        stored,
        report,
        settings=replace(settings, dry_run=False),
        store=store,
        session=session,
        execute=True,
    )
    assert report.skipped_not_approved == 1
    assert report.applied == 0
    assert report.writable == 0
    assert session.posts == []
    assert session.patches == []
    assert store.list_attempts(stored.id) == []
    assert any("no longer approved" in note for note in report.notes)


def test_chow_uncertain_patch_release_identity_lock_after_reconcile(settings, tmp_path):
    store = ProposalStore(tmp_path / "db.sqlite")
    old = _account(
        "OLD1",
        **{fields.PARENT_ID: "WRONG", fields.LIFETIME_REVENUE: 9000, fields.OUTSTANDING_AR: 300},
    )
    session = CrmFake({"OLD1": old, "PARENT": _parent(), "WRONG": _wrong_parent()})
    session.lose_patch_response = 1
    stored = _approve(store, _chow_proposal())

    first = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert first.failed == 1
    assert len(session.posts) == 1
    assert len(session.patches) == 1
    new_id = store.created_account_id_for_proposal(stored.id)
    assert new_id
    assert store.list_attempts(stored.id)[-1].state == STATE_UNCERTAIN
    assert store.has_create_identity_lock_for_proposal(stored.id)
    # PATCH landed remotely even though the response was lost.
    assert session.accounts["OLD1"][fields.CHOW_CURRENT_ACCOUNT] == new_id

    second = _run(settings, store, session, stored, execute=True, dry_run=False)
    assert second.applied == 1
    assert len(session.posts) == 1
    assert store.successful_attempt(stored.id).created_account_id == new_id
    assert not store.has_create_identity_lock_for_proposal(stored.id)

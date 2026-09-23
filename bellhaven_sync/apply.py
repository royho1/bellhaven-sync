"""Apply approved proposals to the CRM.

This is the only module that may POST or PATCH. ``crm_client`` stays GET-only,
and the scheduled sync path never imports this file.

Writes are single-shot. A lost POST response is not retried blindly: the attempt
is recorded as uncertain, and the next run recovers a unique tool-created account
before linking. CHOW saves the new account id before linking the old account, so
a later run can finish the link without creating a duplicate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests

from . import crm_client, fields
from .config import DEFAULT_TIMEOUT, Settings, redact
from .proposals import (
    ACTION_CHOW,
    ACTION_CREATE_ACCOUNT,
    ACTION_REPARENT,
    ACTION_REVIEW_AMBIGUOUS,
    ACTION_REVIEW_CARE_TYPE,
    ACTION_REVIEW_CHOW,
    ACTION_REVIEW_DUPLICATE,
    ACTION_REVIEW_INACTIVE,
    ACTION_REVIEW_STALE,
    ACTION_UPDATE_FIELDS,
    SUPPORTED_CARE_TYPES,
)
from .store import (
    CLAIM_ALREADY_APPLIED,
    CLAIM_CLAIMED,
    CLAIM_IN_PROGRESS,
    MODE_DRY_RUN,
    MODE_EXECUTE,
    STATE_APPLIED,
    STATE_BLOCKED,
    STATE_IN_PROGRESS,
    STATE_PLANNED,
    STATE_UNCERTAIN,
    ProposalStore,
    StoredProposal,
)

WRITABLE_ACTIONS = frozenset(
    {ACTION_UPDATE_FIELDS, ACTION_REPARENT, ACTION_CREATE_ACCOUNT, ACTION_CHOW}
)
REVIEW_ONLY_ACTIONS = frozenset(
    {
        ACTION_REVIEW_AMBIGUOUS,
        ACTION_REVIEW_DUPLICATE,
        ACTION_REVIEW_STALE,
        ACTION_REVIEW_CHOW,
        ACTION_REVIEW_INACTIVE,
        ACTION_REVIEW_CARE_TYPE,
    }
)

UPDATE_FIELDS_ALLOW = frozenset(
    {
        fields.NAME,
        fields.STREET,
        fields.CITY,
        fields.STATE,
        fields.ZIP,
        fields.CARE_TYPE,
        fields.PHONE,
    }
)
CREATE_FIELDS_ALLOW = UPDATE_FIELDS_ALLOW | {fields.PARENT_ID, fields.STATUS}
CREATE_STATUS_ALLOW = frozenset({fields.STATUS_ACTIVE, fields.STATUS_INACTIVE})
SUPPORTED_CARE = frozenset(SUPPORTED_CARE_TYPES)
CREATE_IDENTITY_FIELDS = (fields.NAME, fields.STREET, fields.CITY, fields.STATE, fields.ZIP)
IN_PROGRESS_CLAIM_MESSAGE = (
    "A previous execute attempt is still in_progress. It may have been interrupted "
    "after a CRM write. Refusing another execution until the prior attempt is reconciled."
)
CREATE_IDENTITY_BUSY_MESSAGE = (
    "Another execute attempt is already creating an account with this identity. "
    "Refusing a concurrent create until the prior attempt finishes or is reconciled."
)


class ApplyError(RuntimeError):
    """A write was refused or did not complete. The message is redacted."""

    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(redact(message))
        self.uncertain = uncertain


class ExecuteWhileDryRunError(ApplyError):
    """``--execute`` was asked for while DRY_RUN is still true."""


@dataclass
class ApplyReport:
    run_id: int | None
    mode: str
    approved: int = 0
    writable: int = 0
    skipped_not_approved: int = 0
    skipped_review: int = 0
    skipped_applied: int = 0
    blocked: int = 0
    failed: int = 0
    applied: int = 0
    planned_posts: int = 0
    planned_patches: int = 0
    notes: list[str] = field(default_factory=list)


def format_apply_report(report: ApplyReport) -> str:
    banner = "EXECUTE MODE" if report.mode == MODE_EXECUTE else "DRY RUN"
    lines = [
        banner,
        f"Run id:                  {report.run_id}",
        f"Approved proposals:      {report.approved}",
        f"Writable approved:       {report.writable}",
        f"Skipped, not approved:   {report.skipped_not_approved}",
        f"Skipped, review-only:    {report.skipped_review}",
        f"Skipped, already applied:{report.skipped_applied}",
        f"Blocked:                 {report.blocked}",
        f"Failed:                  {report.failed}",
        f"Applied:                 {report.applied}",
        f"Planned POSTs:           {report.planned_posts}",
        f"Planned PATCHes:         {report.planned_patches}",
    ]
    lines.extend(report.notes)
    if report.mode == MODE_DRY_RUN:
        lines.append("No CRM writes were attempted.")
    return "\n".join(lines)


def run_apply(
    *,
    settings: Settings,
    store: ProposalStore,
    session: Any | None = None,
    run_id: int | None = None,
    proposal_id: int | None = None,
    execute: bool = False,
) -> ApplyReport:
    """Plan or apply approved proposals for one reconciliation run."""
    if execute and settings.dry_run:
        raise ExecuteWhileDryRunError(
            "Refusing to write: --execute was set but DRY_RUN is still true. "
            "Set DRY_RUN=false and pass --execute. Neither gate alone writes."
        )
    if execute and session is None:
        raise ApplyError("Execute mode requires an HTTP session.")

    selected = run_id if run_id is not None else store.latest_run_id()
    report = ApplyReport(run_id=selected, mode=MODE_EXECUTE if execute else MODE_DRY_RUN)
    if selected is None:
        report.notes.append("No reconciliation run exists.")
        return report

    proposals = store.list_proposals(run_id=selected)
    if proposal_id is not None:
        proposals = [item for item in proposals if item.id == proposal_id]
        if not proposals:
            raise ApplyError(
                f"Proposal {proposal_id} is not in run {selected}. "
                "Refusing to apply a proposal from another run."
            )

    for proposal in proposals:
        _consider(
            proposal,
            report,
            settings=settings,
            store=store,
            session=session,
            execute=execute,
        )
    return report


def _consider(
    proposal: StoredProposal,
    report: ApplyReport,
    *,
    settings: Settings,
    store: ProposalStore,
    session: Any | None,
    execute: bool,
) -> None:
    if proposal.status != "approved":
        report.skipped_not_approved += 1
        report.notes.append(f"#{proposal.id} {proposal.action_type}: not approved ({proposal.status})")
        return

    report.approved += 1
    if proposal.action_type in REVIEW_ONLY_ACTIONS:
        report.skipped_review += 1
        report.notes.append(f"#{proposal.id} {proposal.action_type}: review-only, no CRM write")
        return
    if proposal.action_type not in WRITABLE_ACTIONS:
        _record_terminal(
            store,
            proposal,
            report,
            execute=execute,
            state=STATE_BLOCKED,
            note=f"#{proposal.id}: unknown action {proposal.action_type!r}; fail closed",
        )
        return

    posts, patches = _planned_calls(proposal.action_type)

    if not execute:
        if store.successful_attempt(proposal.id) is not None:
            report.skipped_applied += 1
            report.notes.append(f"#{proposal.id} {proposal.action_type}: already applied")
            return
        try:
            _validate_payload(proposal)
        except ApplyError as exc:
            _record_terminal(
                store,
                proposal,
                report,
                execute=False,
                state=STATE_BLOCKED,
                note=f"#{proposal.id} {proposal.action_type}: blocked: {exc}",
            )
            return
        report.writable += 1
        report.planned_posts += posts
        report.planned_patches += patches
        store.record_attempt(
            proposal_id=proposal.id,
            run_id=proposal.run_id,
            action_type=proposal.action_type,
            mode=MODE_DRY_RUN,
            state=STATE_PLANNED,
        )
        report.notes.append(
            f"#{proposal.id} {proposal.action_type}: planned ({posts} POST, {patches} PATCH)"
        )
        return

    try:
        _validate_payload(proposal)
    except ApplyError as exc:
        _record_terminal(
            store,
            proposal,
            report,
            execute=True,
            state=STATE_BLOCKED,
            note=f"#{proposal.id} {proposal.action_type}: blocked: {exc}",
        )
        return

    claim = store.claim_execute_attempt(
        proposal_id=proposal.id,
        run_id=proposal.run_id,
        action_type=proposal.action_type,
    )
    if claim.status == CLAIM_ALREADY_APPLIED:
        report.skipped_applied += 1
        report.notes.append(f"#{proposal.id} {proposal.action_type}: already applied")
        return
    if claim.status == CLAIM_IN_PROGRESS:
        report.blocked += 1
        report.notes.append(f"#{proposal.id} {proposal.action_type}: blocked: {IN_PROGRESS_CLAIM_MESSAGE}")
        return
    if claim.status != CLAIM_CLAIMED or claim.attempt is None:
        report.blocked += 1
        report.notes.append(f"#{proposal.id} {proposal.action_type}: blocked: execute claim failed")
        return

    report.writable += 1
    report.planned_posts += posts
    report.planned_patches += patches
    attempt = claim.attempt
    try:
        created = _execute(proposal, settings=settings, store=store, session=session, attempt_id=attempt.id)
    except ApplyError as exc:
        if exc.uncertain:
            state = STATE_UNCERTAIN
        else:
            state = STATE_BLOCKED
        store.update_attempt(attempt.id, state=state, error_text=str(exc))
        if state == STATE_BLOCKED:
            report.blocked += 1
        else:
            report.failed += 1
        report.notes.append(f"#{proposal.id} {proposal.action_type}: {state}: {exc}")
        return

    store.update_attempt(attempt.id, state=STATE_APPLIED, created_account_id=created)
    report.applied += 1
    report.notes.append(f"#{proposal.id} {proposal.action_type}: applied")


def _record_terminal(
    store: ProposalStore,
    proposal: StoredProposal,
    report: ApplyReport,
    *,
    execute: bool,
    state: str,
    note: str,
) -> None:
    report.blocked += 1
    report.notes.append(note)
    if execute:
        store.record_attempt(
            proposal_id=proposal.id,
            run_id=proposal.run_id,
            action_type=proposal.action_type,
            mode=MODE_EXECUTE,
            state=state,
            error_text=note,
        )


def _planned_calls(action_type: str) -> tuple[int, int]:
    if action_type == ACTION_CREATE_ACCOUNT:
        return 1, 0
    if action_type == ACTION_CHOW:
        return 1, 1
    if action_type in {ACTION_UPDATE_FIELDS, ACTION_REPARENT}:
        return 0, 1
    return 0, 0


def _validate_payload(proposal: StoredProposal) -> None:
    if proposal.action_type == ACTION_UPDATE_FIELDS:
        _reject_unexpected(proposal.proposed_values, UPDATE_FIELDS_ALLOW, "update_fields")
        _reject_bad_care_type(proposal.proposed_values)
        if not proposal.account_id:
            raise ApplyError("update_fields is missing account_id")
        if not proposal.proposed_values:
            raise ApplyError("update_fields has no fields to write")
        return
    if proposal.action_type == ACTION_REPARENT:
        parent = proposal.proposed_values.get(fields.PARENT_ID)
        if set(proposal.proposed_values) != {fields.PARENT_ID} or not parent:
            raise ApplyError("reparent payload must be exactly parent_id")
        if not proposal.account_id:
            raise ApplyError("reparent is missing account_id")
        return
    if proposal.action_type == ACTION_CREATE_ACCOUNT:
        _validate_create_body(proposal.proposed_values)
        return
    if proposal.action_type == ACTION_CHOW:
        template = proposal.proposed_values.get("new_account_template")
        parent = proposal.proposed_values.get("new_account_parent_id")
        patch = proposal.proposed_values.get("old_account_patch")
        if not isinstance(template, dict) or not parent:
            raise ApplyError("CHOW proposal is missing new_account_template or parent")
        if not isinstance(patch, dict) or set(patch) != {fields.CHOW_CURRENT_ACCOUNT}:
            raise ApplyError("CHOW old_account_patch must contain only chow_current_account")
        if not proposal.account_id:
            raise ApplyError("CHOW is missing the old account_id")
        _validate_create_body({**template, fields.PARENT_ID: parent})
        return
    raise ApplyError(f"unknown action {proposal.action_type!r}")


def _validate_create_body(body: dict[str, Any]) -> None:
    _reject_unexpected(body, CREATE_FIELDS_ALLOW, "create")
    _reject_bad_care_type(body)
    status = body.get(fields.STATUS)
    if status is not None and status not in CREATE_STATUS_ALLOW:
        raise ApplyError(f"create status {status!r} is not an observed CRM status")
    if not body.get(fields.PARENT_ID):
        raise ApplyError("create is missing parent_id")
    if not body.get(fields.NAME):
        raise ApplyError("create is missing name")


def _reject_unexpected(body: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    extra = sorted(set(body) - allowed)
    if extra:
        raise ApplyError(f"{label} contains fields that cannot be written: {extra}")


def _reject_bad_care_type(body: dict[str, Any]) -> None:
    if fields.CARE_TYPE not in body:
        return
    care = body[fields.CARE_TYPE]
    if care not in SUPPORTED_CARE:
        raise ApplyError(f"care_type {care!r} is outside the observed CRM domain")


def _execute(
    proposal: StoredProposal,
    *,
    settings: Settings,
    store: ProposalStore,
    session: Any,
    attempt_id: int,
) -> str | None:
    if proposal.action_type == ACTION_UPDATE_FIELDS:
        _patch_existing(proposal, session, settings, dict(proposal.proposed_values))
        return None
    if proposal.action_type == ACTION_REPARENT:
        _require_account(str(proposal.proposed_values[fields.PARENT_ID]), session, settings)
        _patch_existing(
            proposal,
            session,
            settings,
            {fields.PARENT_ID: proposal.proposed_values[fields.PARENT_ID]},
        )
        return None
    if proposal.action_type == ACTION_CREATE_ACCOUNT:
        return _create_account(
            proposal,
            store=store,
            session=session,
            settings=settings,
            attempt_id=attempt_id,
        )
    if proposal.action_type == ACTION_CHOW:
        return _apply_chow(proposal, settings=settings, store=store, session=session, attempt_id=attempt_id)
    raise ApplyError(f"unknown action {proposal.action_type!r}")


def _patch_existing(
    proposal: StoredProposal,
    session: Any,
    settings: Settings,
    body: dict[str, Any],
) -> None:
    live = _require_account(str(proposal.account_id), session, settings)
    _require_fresh(proposal.current_values, live)
    _patch(session, settings, str(proposal.account_id), body)


def _create_account(
    proposal: StoredProposal,
    *,
    store: ProposalStore,
    session: Any,
    settings: Settings,
    attempt_id: int,
) -> str:
    proposed = proposal.proposed_values
    body = {key: proposed[key] for key in CREATE_FIELDS_ALLOW if key in proposed}
    body[fields.CREATED_BY_CANDIDATE] = True
    _require_account(str(body[fields.PARENT_ID]), session, settings)
    _claim_create_identity(store, body, proposal_id=proposal.id, attempt_id=attempt_id)
    recovered = _resolve_create_id(
        body,
        proposal,
        store,
        session,
        settings,
        uncertain_message=(
            "Previous create POST outcome is uncertain and no unique created account "
            "is visible yet. Refusing a second POST."
        ),
        multiple_tool_message=(
            "multiple tool-created accounts match this create; stopping for human review"
        ),
    )
    if recovered is not None:
        return recovered
    payload = _post(session, settings, "/accounts", body)
    account_id = _account_id(payload)
    if not account_id:
        raise ApplyError("create POST returned no account_id; not retrying", uncertain=True)
    return account_id


def _apply_chow(
    proposal: StoredProposal,
    *,
    settings: Settings,
    store: ProposalStore,
    session: Any,
    attempt_id: int,
) -> str:
    old_id = str(proposal.account_id)
    parent_id = str(proposal.proposed_values["new_account_parent_id"])
    template = proposal.proposed_values["new_account_template"]
    remembered = store.created_account_id_for_proposal(proposal.id)
    live = _require_account(old_id, session, settings)
    linked = str(live.get(fields.CHOW_CURRENT_ACCOUNT) or "")

    if remembered and linked == remembered:
        return remembered
    if linked and remembered and linked != remembered:
        raise ApplyError(
            "old account chow_current_account points at a different account; not overwriting"
        )
    if linked and not remembered:
        raise ApplyError(
            "old account already has chow_current_account set; not overwriting"
        )

    _require_fresh(proposal.current_values, live)
    if remembered:
        _require_account(remembered, session, settings)
        new_id = remembered
    else:
        _require_account(parent_id, session, settings)
        create_body = {key: template[key] for key in CREATE_FIELDS_ALLOW if key in template}
        create_body[fields.PARENT_ID] = parent_id
        create_body[fields.CREATED_BY_CANDIDATE] = True
        _claim_create_identity(store, create_body, proposal_id=proposal.id, attempt_id=attempt_id)
        recovered = _resolve_create_id(
            create_body,
            proposal,
            store,
            session,
            settings,
            uncertain_message=(
                "Previous CHOW POST outcome is uncertain and no unique created account "
                "is visible yet. Refusing a second POST."
            ),
            multiple_tool_message=(
                "multiple tool-created accounts match this CHOW create; stopping for human review"
            ),
        )
        if recovered is not None:
            new_id = recovered
            store.update_attempt(attempt_id, state=STATE_IN_PROGRESS, created_account_id=new_id)
        else:
            payload = _post(session, settings, "/accounts", create_body)
            new_id = _account_id(payload)
            if not new_id:
                raise ApplyError(
                    "CHOW POST returned no account_id; old account was not patched",
                    uncertain=True,
                )
            store.update_attempt(attempt_id, state=STATE_IN_PROGRESS, created_account_id=new_id)

    _patch(session, settings, old_id, {fields.CHOW_CURRENT_ACCOUNT: new_id})
    return new_id


@dataclass(frozen=True)
class _CreateMatches:
    tool_matches: list[dict[str, Any]]
    non_tool_matches: list[dict[str, Any]]


def _matching_accounts_for_create(
    body: dict[str, Any],
    session: Any,
    settings: Settings,
) -> _CreateMatches:
    tool_matches: list[dict[str, Any]] = []
    non_tool_matches: list[dict[str, Any]] = []
    try:
        for account in crm_client.iter_accounts(session=session, settings=settings):
            if str(account.get(fields.PARENT_ID) or "") != str(body.get(fields.PARENT_ID) or ""):
                continue
            if not _same_identity(account, body):
                continue
            if account.get(fields.CREATED_BY_CANDIDATE) is True:
                tool_matches.append(account)
            else:
                non_tool_matches.append(account)
    except crm_client.CrmError as exc:
        raise ApplyError(f"could not scan for previously created accounts: {exc}") from None
    return _CreateMatches(tool_matches=tool_matches, non_tool_matches=non_tool_matches)


def _resolve_create_id(
    body: dict[str, Any],
    proposal: StoredProposal,
    store: ProposalStore,
    session: Any,
    settings: Settings,
    *,
    uncertain_message: str,
    multiple_tool_message: str,
) -> str | None:
    """Return a unique tool-created account id to reuse, or None if a first POST is allowed."""
    matches = _matching_accounts_for_create(body, session, settings)
    if matches.non_tool_matches:
        raise ApplyError(
            "A matching CRM account now exists but was not created by this tool. "
            "Refusing to create a duplicate; run sync and review again."
        )
    if len(matches.tool_matches) > 1:
        raise ApplyError(multiple_tool_message)
    if len(matches.tool_matches) == 1:
        account_id = str(matches.tool_matches[0].get(fields.ACCOUNT_ID) or "")
        if not account_id:
            raise ApplyError(
                "recovered create account is missing account_id; stopping for human review"
            )
        return account_id
    if store.has_uncertain_attempt(proposal.id):
        raise ApplyError(uncertain_message)
    return None


def _evidence_text(value: Any) -> str | None:
    """Return stripped text evidence, or None when the proposed value is blank/missing."""
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _create_identity_key(body: dict[str, Any]) -> str:
    """Normalized create identity for cross-proposal execute serialization."""
    parent = str(body.get(fields.PARENT_ID) or "").strip()
    parts = [f"parent={parent}"]
    for key in CREATE_IDENTITY_FIELDS:
        if key not in body:
            continue
        value = _evidence_text(body.get(key))
        if value is None:
            continue
        parts.append(f"{key}={value}")
    return "\n".join(parts)


def _claim_create_identity(
    store: ProposalStore,
    body: dict[str, Any],
    *,
    proposal_id: int,
    attempt_id: int,
) -> None:
    key = _create_identity_key(body)
    if not store.claim_create_identity(
        identity_key=key,
        proposal_id=proposal_id,
        attempt_id=attempt_id,
    ):
        raise ApplyError(CREATE_IDENTITY_BUSY_MESSAGE)


def _same_identity(account: dict[str, Any], body: dict[str, Any]) -> bool:
    """Stable create/recovery identity: name and address only. Phone is ignored.

    Blank or whitespace-only proposed fields are unavailable evidence and are
    skipped, so a missing city/ZIP does not reject an otherwise matching account.
    """
    compared = False
    for key in CREATE_IDENTITY_FIELDS:
        if key not in body:
            continue
        proposed = _evidence_text(body.get(key))
        if proposed is None:
            continue
        compared = True
        if not _same(proposed, account.get(key)):
            return False
    return compared


def _require_fresh(reviewed: dict[str, Any], live: dict[str, Any]) -> None:
    changed = [key for key, expected in reviewed.items() if not _same(expected, live.get(key))]
    if changed:
        raise ApplyError(
            "CRM changed since this proposal was reviewed "
            f"({', '.join(changed)}). No write. Run sync and review again."
        )


def _same(expected: Any, live: Any) -> bool:
    if expected == live:
        return True
    return str(expected if expected is not None else "").strip() == str(live if live is not None else "").strip()


def _require_account(account_id: str, session: Any, settings: Settings) -> dict[str, Any]:
    try:
        payload = crm_client.get_account(account_id, session=session, settings=settings)
    except crm_client.CrmError as exc:
        raise ApplyError(f"could not read account {account_id}: {exc}") from None
    account = _as_account(payload)
    if not account:
        raise ApplyError(f"account {account_id} response was empty")
    return account


def _as_account(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and fields.ACCOUNT_ID in payload:
        return payload
    if isinstance(payload, dict):
        for key in ("data", "account"):
            inner = payload.get(key)
            if isinstance(inner, dict):
                return inner
    return {}


def _account_id(payload: Any) -> str:
    account = _as_account(payload)
    value = account.get(fields.ACCOUNT_ID)
    return str(value) if value else ""


def _post(session: Any, settings: Settings, path: str, body: dict[str, Any]) -> Any:
    url = f"{settings.base_url}/{path.lstrip('/')}"
    try:
        response = session.post(url, json=body, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise ApplyError(
            f"POST {path} failed before a confirmed response: {exc}. Not retrying.",
            uncertain=True,
        ) from None
    return _read_write_response(response, f"POST {path}")


def _patch(session: Any, settings: Settings, account_id: str, body: dict[str, Any]) -> Any:
    url = f"{settings.base_url}/accounts/{account_id}"
    try:
        response = session.patch(url, json=body, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise ApplyError(
            f"PATCH /accounts/{account_id} failed before a confirmed response: {exc}. Not retrying.",
            uncertain=True,
        ) from None
    return _read_write_response(response, f"PATCH /accounts/{account_id}")


def _read_write_response(response: Any, label: str) -> Any:
    status = int(getattr(response, "status_code", 0) or 0)
    text = redact(getattr(response, "text", "") or "")[:300]
    if status >= 500 or status == 0:
        raise ApplyError(f"{label} was not confirmed ({status}): {text}. Not retrying.", uncertain=True)
    if status >= 400:
        raise ApplyError(f"{label} rejected ({status}): {text}")
    try:
        return response.json()
    except ValueError:
        return {}

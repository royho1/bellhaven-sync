"""Reconciliation proposals: conservative, explicit action types only.

Proposals describe what a human might approve later. This module never writes
to the CRM. Field comparison uses normalized values; proposed CRM values keep
the human-readable website originals.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import chow, fields
from .matching import DuplicateGroup, MatchResult, ParentResolution
from .normalize import normalize_name, normalize_street, normalize_zip
from .scraper import Facility, ScrapeResult

ACTION_UPDATE_FIELDS = "update_fields"
ACTION_REPARENT = "reparent"
ACTION_CHOW = "chow_create_and_link"
ACTION_CREATE_ACCOUNT = "create_account"
ACTION_REVIEW_AMBIGUOUS = "review_ambiguous"
ACTION_REVIEW_DUPLICATE = "review_duplicate"
ACTION_REVIEW_STALE = "review_stale_or_missing"
ACTION_REVIEW_CHOW = "review_chow_ambiguous"

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

STATUS_VALUES = frozenset({STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED})

COMPARABLE_FIELDS = (
    fields.NAME,
    fields.STREET,
    fields.CITY,
    fields.STATE,
    fields.ZIP,
    fields.CARE_TYPE,
    fields.PHONE,
)


@dataclass
class Proposal:
    action_type: str
    account_id: str | None
    facility_url: str | None
    current_values: dict[str, Any]
    proposed_values: dict[str, Any]
    evidence: dict[str, Any]
    confidence: str
    requires_review: bool = True
    status: str = STATUS_PENDING
    reason: str = ""

    def to_record(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "account_id": self.account_id,
            "facility_url": self.facility_url,
            "current_values": self.current_values,
            "proposed_values": self.proposed_values,
            "evidence": {
                **self.evidence,
                "reason": self.reason or self.evidence.get("reason", ""),
            },
            "confidence": self.confidence,
            "requires_review": self.requires_review,
            "status": self.status,
        }


@dataclass
class ProposalBatch:
    proposals: list[Proposal] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    scrape_complete: bool = False
    parent_account_id: str | None = None
    generated_at: str = ""

    def add(self, proposal: Proposal) -> None:
        self.proposals.append(proposal)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _facility_care_type(facility: Facility) -> str:
    return ", ".join(facility.care_types) if facility.care_types else ""


def _website_field_values(facility: Facility) -> dict[str, str]:
    return {
        fields.NAME: facility.name or "",
        fields.STREET: facility.street or "",
        fields.CITY: facility.city or "",
        fields.STATE: (facility.state or "").upper(),
        fields.ZIP: facility.zip or "",
        fields.CARE_TYPE: _facility_care_type(facility),
        fields.PHONE: facility.phone or "",
    }


def _account_field_values(account: dict[str, Any]) -> dict[str, Any]:
    return {key: account.get(key, fields.UNSET) for key in COMPARABLE_FIELDS}


def _normalized_equal(field_name: str, left: Any, right: Any) -> bool:
    if field_name == fields.NAME:
        return normalize_name(left) == normalize_name(right)
    if field_name == fields.STREET:
        return normalize_street(left) == normalize_street(right)
    if field_name == fields.ZIP:
        return normalize_zip(left) == normalize_zip(right)
    if field_name == fields.STATE:
        return (str(left or "").strip().upper()[:2]) == (str(right or "").strip().upper()[:2])
    if field_name == fields.CITY:
        return str(left or "").strip().lower() == str(right or "").strip().lower()
    if field_name == fields.CARE_TYPE:
        return str(left or "").strip().lower() == str(right or "").strip().lower()
    if field_name == fields.PHONE:
        left_digits = "".join(ch for ch in str(left or "") if ch.isdigit())
        right_digits = "".join(ch for ch in str(right or "") if ch.isdigit())
        if not left_digits or not right_digits:
            return not left_digits and not right_digits
        return left_digits == right_digits
    return str(left or "").strip() == str(right or "").strip()


def _field_diffs(facility: Facility, account: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    website = _website_field_values(facility)
    current = _account_field_values(account)
    cur_out: dict[str, Any] = {}
    prop_out: dict[str, Any] = {}
    for key in COMPARABLE_FIELDS:
        web_val = website.get(key, "")
        crm_val = current.get(key, "")
        if not web_val and not fields.is_set(crm_val):
            continue
        if not web_val:
            # Website has no value; do not clear CRM fields automatically.
            continue
        if _normalized_equal(key, web_val, crm_val):
            continue
        cur_out[key] = crm_val
        prop_out[key] = web_val
    return cur_out, prop_out


def _is_bellhaven_child(account: dict[str, Any], parent_id: str) -> bool:
    return str(account.get(fields.PARENT_ID) or "") == parent_id


def generate_proposals(
    *,
    scrape: ScrapeResult,
    accounts: list[dict[str, Any]],
    matches: list[MatchResult],
    parent: ParentResolution,
    duplicates: list[DuplicateGroup],
) -> ProposalBatch:
    """Build a conservative proposal batch from scrape + CRM evidence."""
    batch = ProposalBatch(
        scrape_complete=scrape.complete,
        parent_account_id=parent.account_id if parent.resolved else None,
        generated_at=_now(),
        blockers=list(scrape.blockers),
    )
    if parent.blocker:
        batch.blockers.append(parent.blocker)

    parent_ok = parent.resolved
    scrape_ok = scrape.complete

    matched_account_ids: set[str] = set()
    matched_facility_urls: set[str] = set()

    for result in matches:
        facility = result.facility
        if result.ambiguous:
            batch.add(
                Proposal(
                    action_type=ACTION_REVIEW_AMBIGUOUS,
                    account_id=None,
                    facility_url=facility.url,
                    current_values={},
                    proposed_values={},
                    evidence={
                        "tier": result.tier,
                        "name_similarity": result.name_similarity,
                        "reasons": list(result.reasons),
                        "runners_up": [asdict(c) for c in result.runners_up],
                        "facility_name": facility.name,
                    },
                    confidence="ambiguous",
                    requires_review=True,
                    reason="ambiguous match; no automatic account update",
                )
            )
            matched_facility_urls.add(facility.url)
            continue

        if result.account is None:
            continue

        account = result.account
        account_id = str(account.get(fields.ACCOUNT_ID))
        matched_account_ids.add(account_id)
        matched_facility_urls.add(facility.url)

        # Parent / CHOW decisions only when we know the correct Bellhaven parent.
        current_parent = str(account.get(fields.PARENT_ID) or "")
        if parent_ok and parent.account_id and current_parent != parent.account_id:
            decision = chow.decide_parent_change(account, target_parent_id=parent.account_id)
            if decision.kind == chow.KIND_HUMAN_REVIEW:
                batch.add(
                    Proposal(
                        action_type=ACTION_REVIEW_CHOW,
                        account_id=account_id,
                        facility_url=facility.url,
                        current_values={
                            fields.PARENT_ID: current_parent,
                            fields.LIFETIME_REVENUE: decision.lifetime_revenue,
                            fields.OUTSTANDING_AR: decision.outstanding_ar,
                        },
                        proposed_values={fields.PARENT_ID: parent.account_id},
                        evidence={
                            "chow_kind": decision.kind,
                            "match_tier": result.tier,
                            "match_reasons": list(result.reasons),
                        },
                        confidence="review",
                        requires_review=True,
                        reason=decision.reason,
                    )
                )
            elif decision.kind == chow.KIND_CHOW_TWO_STEP:
                batch.add(
                    Proposal(
                        action_type=ACTION_CHOW,
                        account_id=account_id,
                        facility_url=facility.url,
                        current_values={
                            fields.PARENT_ID: current_parent,
                            fields.NAME: account.get(fields.NAME),
                            fields.STREET: account.get(fields.STREET),
                            fields.CITY: account.get(fields.CITY),
                            fields.STATE: account.get(fields.STATE),
                            fields.ZIP: account.get(fields.ZIP),
                            fields.LIFETIME_REVENUE: decision.lifetime_revenue,
                            fields.OUTSTANDING_AR: decision.outstanding_ar,
                        },
                        proposed_values={
                            "new_account_parent_id": parent.account_id,
                            "new_account_template": _website_field_values(facility),
                            "old_account_patch": {fields.CHOW_CURRENT_ACCOUNT: "<new_account_id>"},
                            "old_account_unchanged": [
                                fields.PARENT_ID,
                                fields.STATUS,
                                fields.NOTE,
                                fields.NAME,
                                fields.STREET,
                                fields.CITY,
                                fields.STATE,
                                fields.ZIP,
                            ],
                        },
                        evidence={
                            "chow_kind": decision.kind,
                            "match_tier": result.tier,
                            "match_reasons": list(result.reasons),
                        },
                        confidence=result.confidence,
                        requires_review=True,
                        reason=decision.reason,
                    )
                )
            else:
                batch.add(
                    Proposal(
                        action_type=ACTION_REPARENT,
                        account_id=account_id,
                        facility_url=facility.url,
                        current_values={fields.PARENT_ID: current_parent},
                        proposed_values={fields.PARENT_ID: parent.account_id},
                        evidence={
                            "chow_kind": decision.kind,
                            "match_tier": result.tier,
                            "match_reasons": list(result.reasons),
                            "lifetime_revenue": decision.lifetime_revenue,
                            "outstanding_ar": decision.outstanding_ar,
                        },
                        confidence=result.confidence,
                        requires_review=True,
                        reason=decision.reason,
                    )
                )

        current_diff, proposed_diff = _field_diffs(facility, account)
        if current_diff:
            batch.add(
                Proposal(
                    action_type=ACTION_UPDATE_FIELDS,
                    account_id=account_id,
                    facility_url=facility.url,
                    current_values=current_diff,
                    proposed_values=proposed_diff,
                    evidence={
                        "match_tier": result.tier,
                        "match_confidence": result.confidence,
                        "match_reasons": list(result.reasons),
                        "name_similarity": result.name_similarity,
                        "facility_name": facility.name,
                    },
                    confidence=result.confidence,
                    requires_review=True,
                    reason="website values differ from CRM on normalized comparison",
                )
            )

    # Unmatched website facilities → create only when scrape + parent are trusted.
    for result in matches:
        if result.account is not None or result.ambiguous:
            continue
        facility = result.facility
        if facility.url in matched_facility_urls:
            continue
        if not scrape_ok or not parent_ok:
            continue
        batch.add(
            Proposal(
                action_type=ACTION_CREATE_ACCOUNT,
                account_id=None,
                facility_url=facility.url,
                current_values={},
                proposed_values={
                    **_website_field_values(facility),
                    fields.PARENT_ID: parent.account_id,
                    fields.STATUS: fields.STATUS_ACTIVE,
                },
                evidence={
                    "facility_name": facility.name,
                    "sources": list(facility.sources),
                    "match_reasons": list(result.reasons),
                },
                confidence="medium",
                requires_review=True,
                reason="website facility has no safe CRM match; propose new Bellhaven child",
            )
        )

    # Possible stale Bellhaven children: only when scrape is complete.
    if scrape_ok and parent_ok and parent.account_id:
        for account in accounts:
            account_id = str(account.get(fields.ACCOUNT_ID) or "")
            if not account_id or account_id in matched_account_ids:
                continue
            if account_id == parent.account_id:
                continue
            if not _is_bellhaven_child(account, parent.account_id):
                continue
            batch.add(
                Proposal(
                    action_type=ACTION_REVIEW_STALE,
                    account_id=account_id,
                    facility_url=None,
                    current_values=_account_field_values(account),
                    proposed_values={},
                    evidence={
                        "parent_id": parent.account_id,
                        "status": account.get(fields.STATUS),
                    },
                    confidence="review",
                    requires_review=True,
                    reason=(
                        "Bellhaven CRM child has no website match on a complete "
                        "scrape; review as possible stale/missing-on-site"
                    ),
                )
            )

    for group in duplicates:
        ids = [str(a.get(fields.ACCOUNT_ID)) for a in group.accounts]
        batch.add(
            Proposal(
                action_type=ACTION_REVIEW_DUPLICATE,
                account_id=ids[0] if ids else None,
                facility_url=None,
                current_values={
                    "account_ids": ids,
                    "accounts": [
                        {
                            fields.ACCOUNT_ID: a.get(fields.ACCOUNT_ID),
                            fields.NAME: a.get(fields.NAME),
                            fields.STREET: a.get(fields.STREET),
                            fields.CITY: a.get(fields.CITY),
                            fields.STATE: a.get(fields.STATE),
                            fields.ZIP: a.get(fields.ZIP),
                        }
                        for a in group.accounts
                    ],
                },
                proposed_values={},
                evidence={"duplicate_key": group.key, "duplicate_reason": group.reason},
                confidence="review",
                requires_review=True,
                reason=f"possible duplicate accounts: {group.reason}",
            )
        )

    batch.summary = _summarize(batch)
    return batch


def _summarize(batch: ProposalBatch) -> dict[str, int]:
    counts = {
        "proposals": len(batch.proposals),
        "update_fields": 0,
        "create_account": 0,
        "reparent": 0,
        "chow_create_and_link": 0,
        "review_ambiguous": 0,
        "review_duplicate": 0,
        "review_stale_or_missing": 0,
        "review_chow_ambiguous": 0,
        "blockers": len(batch.blockers),
    }
    for proposal in batch.proposals:
        if proposal.action_type in counts:
            counts[proposal.action_type] += 1
    return counts

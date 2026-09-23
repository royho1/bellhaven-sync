"""Proposal generation: conservative actions from scrape + CRM evidence."""

from __future__ import annotations

from bellhaven_sync import fields
from bellhaven_sync.matching import DuplicateGroup, ParentResolution, match_facilities
from bellhaven_sync.proposals import (
    ACTION_CHOW,
    ACTION_CREATE_ACCOUNT,
    ACTION_REPARENT,
    ACTION_REVIEW_AMBIGUOUS,
    ACTION_REVIEW_CHOW,
    ACTION_REVIEW_DUPLICATE,
    ACTION_REVIEW_STALE,
    ACTION_UPDATE_FIELDS,
    generate_proposals,
)
from bellhaven_sync.scraper import Facility, ScrapeResult


def _facility(**kwargs) -> Facility:
    defaults = dict(
        name="Bellhaven of Tiffin",
        url="https://example.test/communities/tiffin",
        street="100 Main Street",
        city="Tiffin",
        state="OH",
        zip="44883",
        phone="419-555-0100",
        care_types=["Assisted Living"],
        sources=["listing"],
    )
    defaults.update(kwargs)
    return Facility(**defaults)


def _account(**kwargs):
    base = {
        fields.ACCOUNT_ID: "C1",
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
    }
    base.update(kwargs)
    return base


def _scrape(facilities, *, complete=True, blockers=None) -> ScrapeResult:
    return ScrapeResult(
        facilities=facilities,
        claimed_count=len(facilities) if complete else (len(facilities) + 5),
        listing_count=len(facilities),
        sitemap_count=len(facilities),
        complete=complete,
        blockers=list(blockers or ([] if complete else ["incomplete"])),
    )


def _parent(resolved=True) -> ParentResolution:
    if resolved:
        return ParentResolution(account_id="PARENT", candidates=[_account(**{fields.ACCOUNT_ID: "PARENT"})])
    return ParentResolution(account_id=None, candidates=[], blocker="No Bellhaven parent account found.")


def test_matched_field_difference_creates_update_proposal():
    facility = _facility(street="100 Main St", phone="419-555-9999")
    account = _account(phone="419-555-0100")
    matches = match_facilities([facility], [account])
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[account],
        matches=matches,
        parent=_parent(),
        duplicates=[],
    )
    updates = [p for p in batch.proposals if p.action_type == ACTION_UPDATE_FIELDS]
    assert len(updates) == 1
    assert updates[0].proposed_values[fields.PHONE] == "419-555-9999"
    assert updates[0].current_values[fields.PHONE] == "419-555-0100"


def test_exact_match_with_no_differences_creates_no_update():
    facility = _facility()
    account = _account()
    matches = match_facilities([facility], [account])
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[account],
        matches=matches,
        parent=_parent(),
        duplicates=[],
    )
    assert all(p.action_type != ACTION_UPDATE_FIELDS for p in batch.proposals)


def test_unmatched_website_facility_creates_account_when_complete():
    facility = _facility(name="Brand New Place", street="9 Pine St", zip="44880")
    parent_acct = _account(**{fields.ACCOUNT_ID: "PARENT", fields.PARENT_ID: "", fields.NAME: "Bellhaven Senior Living (Parent Account)"})
    batch = generate_proposals(
        scrape=_scrape([facility], complete=True),
        accounts=[parent_acct],
        matches=match_facilities([facility], [parent_acct]),
        parent=_parent(),
        duplicates=[],
    )
    creates = [p for p in batch.proposals if p.action_type == ACTION_CREATE_ACCOUNT]
    assert len(creates) == 1
    assert creates[0].proposed_values[fields.PARENT_ID] == "PARENT"
    assert creates[0].proposed_values[fields.NAME] == "Brand New Place"


def test_incomplete_scrape_suppresses_create_and_stale():
    facility = _facility(name="Brand New Place", street="9 Pine St", zip="44880")
    parent_acct = _account(**{fields.ACCOUNT_ID: "PARENT", fields.PARENT_ID: ""})
    orphan = _account(**{fields.ACCOUNT_ID: "ORPHAN", fields.PARENT_ID: "PARENT", fields.STREET: "1 Other St", fields.ZIP: "44881"})
    batch = generate_proposals(
        scrape=_scrape([facility], complete=False),
        accounts=[parent_acct, orphan],
        matches=match_facilities([facility], [parent_acct, orphan]),
        parent=_parent(),
        duplicates=[],
    )
    actions = {p.action_type for p in batch.proposals}
    assert ACTION_CREATE_ACCOUNT not in actions
    assert ACTION_REVIEW_STALE not in actions


def test_ambiguous_match_is_review_only():
    facility = _facility()
    a1 = _account(**{fields.ACCOUNT_ID: "A1", fields.NAME: "Bellhaven of Tiffin East"})
    a2 = _account(**{fields.ACCOUNT_ID: "A2", fields.NAME: "Bellhaven of Tiffin West"})
    matches = match_facilities([facility], [a1, a2])
    assert matches[0].ambiguous
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[a1, a2],
        matches=matches,
        parent=_parent(),
        duplicates=[],
    )
    assert any(p.action_type == ACTION_REVIEW_AMBIGUOUS for p in batch.proposals)
    assert all(p.action_type != ACTION_UPDATE_FIELDS for p in batch.proposals)


def test_duplicate_group_is_review_only():
    a1 = _account(**{fields.ACCOUNT_ID: "D1"})
    a2 = _account(**{fields.ACCOUNT_ID: "D2", fields.NAME: "Bellhaven of Tiffin Copy"})
    group = DuplicateGroup(key="k", reason="same state, house number, street, and ZIP", accounts=[a1, a2])
    batch = generate_proposals(
        scrape=_scrape([]),
        accounts=[a1, a2],
        matches=[],
        parent=_parent(),
        duplicates=[group],
    )
    dups = [p for p in batch.proposals if p.action_type == ACTION_REVIEW_DUPLICATE]
    assert len(dups) == 1
    assert dups[0].current_values["account_ids"] == ["D1", "D2"]


def test_unresolved_parent_suppresses_create_and_parent_moves():
    facility = _facility()
    account = _account(**{fields.PARENT_ID: "WRONG", fields.LIFETIME_REVENUE: 0, fields.OUTSTANDING_AR: 0})
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[account],
        matches=match_facilities([facility], [account]),
        parent=_parent(resolved=False),
        duplicates=[],
    )
    actions = {p.action_type for p in batch.proposals}
    assert ACTION_CREATE_ACCOUNT not in actions
    assert ACTION_REPARENT not in actions
    assert ACTION_CHOW not in actions
    assert any("parent" in b.lower() for b in batch.blockers)


def test_direct_reparent_proposal():
    facility = _facility()
    account = _account(**{fields.PARENT_ID: "WRONG", fields.LIFETIME_REVENUE: 100, fields.OUTSTANDING_AR: 0})
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[account],
        matches=match_facilities([facility], [account]),
        parent=_parent(),
        duplicates=[],
    )
    reparents = [p for p in batch.proposals if p.action_type == ACTION_REPARENT]
    assert len(reparents) == 1
    assert reparents[0].proposed_values[fields.PARENT_ID] == "PARENT"


def test_chow_two_step_proposal():
    facility = _facility()
    account = _account(**{fields.PARENT_ID: "WRONG", fields.LIFETIME_REVENUE: 9000, fields.OUTSTANDING_AR: 300})
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[account],
        matches=match_facilities([facility], [account]),
        parent=_parent(),
        duplicates=[],
    )
    chows = [p for p in batch.proposals if p.action_type == ACTION_CHOW]
    assert len(chows) == 1
    assert "new_account_parent_id" in chows[0].proposed_values
    assert fields.CHOW_CURRENT_ACCOUNT in chows[0].proposed_values["old_account_patch"]
    assert fields.PARENT_ID in chows[0].proposed_values["old_account_unchanged"]


def test_ambiguous_chow_routes_to_review():
    facility = _facility()
    account = _account(**{fields.PARENT_ID: "WRONG", fields.LIFETIME_REVENUE: 0, fields.OUTSTANDING_AR: 50})
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[account],
        matches=match_facilities([facility], [account]),
        parent=_parent(),
        duplicates=[],
    )
    assert any(p.action_type == ACTION_REVIEW_CHOW for p in batch.proposals)
    assert all(p.action_type != ACTION_CHOW for p in batch.proposals)
    assert all(p.action_type != ACTION_REPARENT for p in batch.proposals)


def test_stale_child_when_scrape_complete():
    facility = _facility()
    matched = _account(**{fields.ACCOUNT_ID: "MATCHED"})
    stale = _account(
        **{
            fields.ACCOUNT_ID: "STALE",
            fields.NAME: "Bellhaven Ghost",
            fields.STREET: "50 Missing Rd",
            fields.ZIP: "44890",
            fields.PARENT_ID: "PARENT",
        }
    )
    parent_acct = _account(**{fields.ACCOUNT_ID: "PARENT", fields.PARENT_ID: ""})
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[parent_acct, matched, stale],
        matches=match_facilities([facility], [matched]),
        parent=_parent(),
        duplicates=[],
    )
    stales = [p for p in batch.proposals if p.action_type == ACTION_REVIEW_STALE]
    assert len(stales) == 1
    assert stales[0].account_id == "STALE"

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
    ACTION_REVIEW_INACTIVE,
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


def test_formatting_only_unit_difference_is_not_a_street_update():
    facility = _facility(street="100 Main St Suite 200")
    account = _account(**{fields.STREET: "100 Main Street Ste. 200"})
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[account],
        matches=match_facilities([facility], [account]),
        parent=_parent(),
        duplicates=[],
    )
    assert all(p.action_type != ACTION_UPDATE_FIELDS for p in batch.proposals)


def test_different_unit_identifier_proposes_street_update():
    facility = _facility(street="100 Main St Suite 200")
    account = _account(**{fields.STREET: "100 Main St Suite 100"})
    matches = match_facilities([facility], [account])
    assert matches[0].account is not None
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[account],
        matches=matches,
        parent=_parent(),
        duplicates=[],
    )
    updates = [p for p in batch.proposals if p.action_type == ACTION_UPDATE_FIELDS]
    assert len(updates) == 1
    assert updates[0].current_values[fields.STREET] == "100 Main St Suite 100"
    assert updates[0].proposed_values[fields.STREET] == "100 Main St Suite 200"


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


def test_unresolved_parent_suppresses_matched_field_updates():
    """Ownership is unknown, so a website diff must not become an approvable field write."""
    facility = _facility(phone="419-555-9999")
    account = _account(
        **{
            fields.PARENT_ID: "WRONG",
            fields.PHONE: "419-555-0100",
            fields.LIFETIME_REVENUE: 9000,
            fields.OUTSTANDING_AR: 300,
        }
    )
    matches = match_facilities([facility], [account])
    assert matches[0].account is not None
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[account],
        matches=matches,
        parent=_parent(resolved=False),
        duplicates=[],
    )
    assert all(p.action_type != ACTION_UPDATE_FIELDS for p in batch.proposals)
    assert all(p.action_type != ACTION_REPARENT for p in batch.proposals)
    assert all(p.action_type != ACTION_CHOW for p in batch.proposals)
    assert any("parent" in blocker.lower() for blocker in batch.blockers)


def test_resolved_correct_parent_still_emits_field_updates():
    facility = _facility(phone="419-555-9999")
    account = _account(**{fields.PARENT_ID: "PARENT", fields.PHONE: "419-555-0100"})
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[account],
        matches=match_facilities([facility], [account]),
        parent=_parent(),
        duplicates=[],
    )
    updates = [p for p in batch.proposals if p.action_type == ACTION_UPDATE_FIELDS]
    assert len(updates) == 1
    assert updates[0].account_id == account[fields.ACCOUNT_ID]
    assert updates[0].proposed_values[fields.PHONE] == "419-555-9999"
    assert all(p.action_type not in {ACTION_REPARENT, ACTION_CHOW} for p in batch.proposals)


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


def test_chow_two_step_suppresses_old_account_field_updates():
    """Website diffs must live in the new-account template, not update the old account."""
    facility = _facility(
        name="Bellhaven of Tiffin Updated",
        phone="419-555-9999",
        care_types=["Memory Care"],
    )
    account = _account(
        **{
            fields.PARENT_ID: "WRONG",
            fields.LIFETIME_REVENUE: 9000,
            fields.OUTSTANDING_AR: 300,
            fields.NAME: "Bellhaven of Tiffin",
            fields.PHONE: "419-555-0100",
            fields.CARE_TYPE: "Assisted Living",
        }
    )
    matches = match_facilities([facility], [account])
    assert matches[0].account is not None
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[account],
        matches=matches,
        parent=_parent(),
        duplicates=[],
    )
    chows = [p for p in batch.proposals if p.action_type == ACTION_CHOW]
    updates = [p for p in batch.proposals if p.action_type == ACTION_UPDATE_FIELDS]
    assert len(chows) == 1
    assert updates == []
    template = chows[0].proposed_values["new_account_template"]
    assert template[fields.NAME] == "Bellhaven of Tiffin Updated"
    assert template[fields.PHONE] == "419-555-9999"
    assert template[fields.CARE_TYPE] == "Memory Care"
    assert template[fields.STREET] == facility.street
    assert chows[0].proposed_values["old_account_patch"] == {
        fields.CHOW_CURRENT_ACCOUNT: "<new_account_id>"
    }
    for protected in (
        fields.PARENT_ID,
        fields.STATUS,
        fields.NOTE,
        fields.NAME,
        fields.STREET,
        fields.CITY,
        fields.STATE,
        fields.ZIP,
    ):
        assert protected in chows[0].proposed_values["old_account_unchanged"]


def test_reparent_still_allows_field_updates():
    facility = _facility(phone="419-555-9999")
    account = _account(
        **{
            fields.PARENT_ID: "WRONG",
            fields.LIFETIME_REVENUE: 100,
            fields.OUTSTANDING_AR: 0,
            fields.PHONE: "419-555-0100",
        }
    )
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[account],
        matches=match_facilities([facility], [account]),
        parent=_parent(),
        duplicates=[],
    )
    assert any(p.action_type == ACTION_REPARENT for p in batch.proposals)
    updates = [p for p in batch.proposals if p.action_type == ACTION_UPDATE_FIELDS]
    assert len(updates) == 1
    assert updates[0].proposed_values[fields.PHONE] == "419-555-9999"


def test_unresolved_chow_suppresses_field_updates_until_review():
    """Human review of ownership must block independent old-account field writes."""
    facility = _facility(name="Bellhaven of Tiffin Updated", phone="419-555-9999")
    account = _account(
        **{
            fields.PARENT_ID: "WRONG",
            fields.LIFETIME_REVENUE: 0,
            fields.OUTSTANDING_AR: 50,
            fields.NAME: "Bellhaven of Tiffin",
            fields.PHONE: "419-555-0100",
        }
    )
    matches = match_facilities([facility], [account])
    assert matches[0].account is not None
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[account],
        matches=matches,
        parent=_parent(),
        duplicates=[],
    )
    reviews = [p for p in batch.proposals if p.action_type == ACTION_REVIEW_CHOW]
    assert len(reviews) == 1
    assert reviews[0].account_id == account[fields.ACCOUNT_ID]
    assert all(p.action_type != ACTION_UPDATE_FIELDS for p in batch.proposals)
    assert all(p.action_type != ACTION_REPARENT for p in batch.proposals)
    assert all(p.action_type != ACTION_CHOW for p in batch.proposals)


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


def test_ambiguous_candidates_are_not_marked_stale():
    facility = _facility()
    a1 = _account(**{fields.ACCOUNT_ID: "A1", fields.NAME: "Bellhaven of Tiffin East"})
    a2 = _account(**{fields.ACCOUNT_ID: "A2", fields.NAME: "Bellhaven of Tiffin West"})
    parent_acct = _account(**{fields.ACCOUNT_ID: "PARENT", fields.PARENT_ID: ""})
    matches = match_facilities([facility], [a1, a2])
    assert matches[0].ambiguous
    batch = generate_proposals(
        scrape=_scrape([facility], complete=True),
        accounts=[parent_acct, a1, a2],
        matches=matches,
        parent=_parent(),
        duplicates=[],
    )
    ambiguous = [p for p in batch.proposals if p.action_type == ACTION_REVIEW_AMBIGUOUS]
    assert len(ambiguous) == 1
    stale_ids = {
        p.account_id for p in batch.proposals if p.action_type == ACTION_REVIEW_STALE
    }
    assert "A1" not in stale_ids
    assert "A2" not in stale_ids
    assert all(
        p.action_type
        not in {ACTION_UPDATE_FIELDS, ACTION_REPARENT, ACTION_CHOW, ACTION_CREATE_ACCOUNT}
        or p.account_id not in {"A1", "A2"}
        for p in batch.proposals
    )
    assert all(
        p.action_type != ACTION_UPDATE_FIELDS and p.action_type != ACTION_REPARENT
        for p in batch.proposals
    )


def test_every_ambiguous_candidate_is_retained_beyond_display_runners():
    """Five plausible accounts must all stay website-associated, not just the display subset."""
    facility = _facility()
    children = [
        _account(
            **{
                fields.ACCOUNT_ID: f"A{i}",
                fields.NAME: f"Bellhaven of Tiffin {i}",
                fields.PARENT_ID: "PARENT",
            }
        )
        for i in range(1, 6)
    ]
    parent_acct = _account(
        **{
            fields.ACCOUNT_ID: "PARENT",
            fields.PARENT_ID: "",
            fields.NAME: "Bellhaven Senior Living",
            fields.STREET: "1 Parent Way",
            fields.CITY: "Columbus",
            fields.ZIP: "43004",
        }
    )
    matches = match_facilities([facility], [*children, parent_acct])
    result = next(match for match in matches if match.facility.url == facility.url)
    expected_ids = {f"A{i}" for i in range(1, 6)}
    assert result.ambiguous is True
    assert result.account is None
    assert set(result.candidate_account_ids) == expected_ids
    runner_ids = {candidate.account_id for candidate in result.runners_up}
    assert runner_ids < expected_ids
    assert expected_ids - runner_ids

    batch = generate_proposals(
        scrape=_scrape([facility], complete=True),
        accounts=[parent_acct, *children],
        matches=matches,
        parent=_parent(),
        duplicates=[],
    )
    ambiguous = [p for p in batch.proposals if p.action_type == ACTION_REVIEW_AMBIGUOUS]
    assert len(ambiguous) == 1
    assert set(ambiguous[0].evidence["candidate_account_ids"]) == expected_ids
    stale_ids = {p.account_id for p in batch.proposals if p.action_type == ACTION_REVIEW_STALE}
    assert stale_ids.isdisjoint(expected_ids)
    automatic = {ACTION_UPDATE_FIELDS, ACTION_REPARENT, ACTION_CHOW}
    assert all(p.action_type not in automatic for p in batch.proposals)


def _proposes_status(proposal) -> bool:
    values = proposal.proposed_values
    if fields.STATUS in values:
        return True
    template = values.get("new_account_template")
    if isinstance(template, dict) and fields.STATUS in template:
        return True
    patch = values.get("old_account_patch")
    return isinstance(patch, dict) and fields.STATUS in patch


def test_matched_inactive_account_is_review_only():
    facility = _facility()
    account = _account(**{fields.STATUS: fields.STATUS_INACTIVE})
    matches = match_facilities([facility], [account])
    assert matches[0].account is not None
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[account],
        matches=matches,
        parent=_parent(),
        duplicates=[],
    )
    inactive = [p for p in batch.proposals if p.action_type == ACTION_REVIEW_INACTIVE]
    assert len(inactive) == 1
    assert len(batch.proposals) == 1
    review = inactive[0]
    assert review.account_id == account[fields.ACCOUNT_ID]
    assert review.facility_url == facility.url
    assert review.current_values[fields.STATUS] == fields.STATUS_INACTIVE
    assert review.evidence["status"] == fields.STATUS_INACTIVE
    assert review.evidence["facility_name"] == facility.name
    assert review.proposed_values == {}
    assert review.requires_review is True
    assert "inactive" in review.reason
    assert batch.summary["review_inactive_account"] == 1
    assert all(not _proposes_status(p) for p in batch.proposals)


def test_matched_active_account_has_no_inactive_review():
    facility = _facility()
    account = _account(**{fields.STATUS: fields.STATUS_ACTIVE})
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[account],
        matches=match_facilities([facility], [account]),
        parent=_parent(),
        duplicates=[],
    )
    assert all(p.action_type != ACTION_REVIEW_INACTIVE for p in batch.proposals)
    assert batch.summary["review_inactive_account"] == 0


def test_inactive_match_with_field_diff_keeps_both_reviews():
    facility = _facility(phone="419-555-9999")
    account = _account(
        **{
            fields.STATUS: fields.STATUS_INACTIVE,
            fields.PARENT_ID: "PARENT",
            fields.PHONE: "419-555-0100",
        }
    )
    batch = generate_proposals(
        scrape=_scrape([facility]),
        accounts=[account],
        matches=match_facilities([facility], [account]),
        parent=_parent(),
        duplicates=[],
    )
    inactive = [p for p in batch.proposals if p.action_type == ACTION_REVIEW_INACTIVE]
    updates = [p for p in batch.proposals if p.action_type == ACTION_UPDATE_FIELDS]
    assert len(inactive) == 1
    assert inactive[0].current_values[fields.STATUS] == fields.STATUS_INACTIVE
    assert inactive[0].proposed_values == {}
    assert len(updates) == 1
    assert updates[0].proposed_values == {fields.PHONE: "419-555-9999"}
    assert fields.STATUS not in updates[0].proposed_values
    assert all(not _proposes_status(p) for p in batch.proposals)

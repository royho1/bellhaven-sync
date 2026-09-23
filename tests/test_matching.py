from __future__ import annotations

from bellhaven_sync import fields
from bellhaven_sync.matching import (
    TIER_STREET_ZIP,
    find_duplicates,
    match_facilities,
    resolve_parent,
)
from bellhaven_sync.scraper import Facility


def _account(**kwargs):
    base = {
        fields.ACCOUNT_ID: "id",
        fields.NAME: "Account",
        fields.PARENT_ID: "",
        fields.STREET: "",
        fields.CITY: "",
        fields.STATE: "",
        fields.ZIP: "",
        fields.STATUS: "Active",
    }
    base.update(kwargs)
    return base


def _facility(**kwargs):
    defaults = dict(name="Facility", url="https://example.test/communities/facility")
    defaults.update(kwargs)
    return Facility(**defaults)


def test_care_qualifier_in_name_does_not_block_address_match():
    facility = _facility(
        name="Bellhaven Memory Care of Tiffin",
        street="100 Main St",
        city="Tiffin",
        state="OH",
        zip="44883",
    )
    account = _account(
        **{
            fields.ACCOUNT_ID: "N1",
            fields.NAME: "Bellhaven Assisted Living of Tiffin",
            fields.STREET: "100 Main Street",
            fields.CITY: "Tiffin",
            fields.STATE: "OH",
            fields.ZIP: "44883",
        }
    )
    results = match_facilities([facility], [account])
    assert results[0].ambiguous is False
    assert results[0].account is not None
    assert results[0].account[fields.ACCOUNT_ID] == "N1"


def test_unit_identifier_difference_does_not_break_match():
    facility = _facility(
        name="Bellhaven of Tiffin",
        street="100 Main St Suite 200",
        city="Tiffin",
        state="OH",
        zip="44883",
    )
    account = _account(
        **{
            fields.ACCOUNT_ID: "U1",
            fields.NAME: "Bellhaven of Tiffin",
            fields.STREET: "100 Main St Suite 100",
            fields.CITY: "Tiffin",
            fields.STATE: "OH",
            fields.ZIP: "44883",
        }
    )
    results = match_facilities([facility], [account])
    assert results[0].ambiguous is False
    assert results[0].account is not None
    assert results[0].account[fields.ACCOUNT_ID] == "U1"
    assert results[0].tier == TIER_STREET_ZIP


def test_tier1_exact_street_and_zip():
    facility = _facility(
        name="Bellhaven Meadows of Findlay",
        street="1800 N Blanchard St",
        city="Findlay",
        state="OH",
        zip="45840",
    )
    account = _account(
        **{
            fields.ACCOUNT_ID: "A1",
            fields.NAME: "Bellhaven Meadows of Findlay",
            fields.STREET: "1800 North Blanchard Street",
            fields.CITY: "Findlay",
            fields.STATE: "OH",
            fields.ZIP: "45840",
        }
    )

    results = match_facilities([facility], [account])
    assert results[0].account["account_id"] == "A1"
    assert results[0].tier == TIER_STREET_ZIP
    assert results[0].confidence == "high"


def test_same_street_different_house_number_does_not_tier1_match():
    # House numbers live outside `street` after normalization; tiers 1 and 2
    # must still require them to match or 100 Main and 200 Main collide.
    facility = _facility(
        name="Bellhaven Alpha",
        street="100 Main St",
        city="Dayton",
        state="OH",
        zip="45402",
    )
    account = _account(
        **{
            fields.ACCOUNT_ID: "B1",
            fields.NAME: "Bellhaven Beta",
            fields.STREET: "200 Main Street",
            fields.CITY: "Dayton",
            fields.STATE: "OH",
            fields.ZIP: "45402",
        }
    )

    results = match_facilities([facility], [account])
    assert results[0].account is None
    assert results[0].confidence == "none"


def test_duplicate_grouping_requires_same_house_number():
    a1 = _account(
        **{
            fields.ACCOUNT_ID: "O1",
            fields.NAME: "Bellhaven of Owosso",
            fields.STREET: "1120 W Main St",
            fields.CITY: "Owosso",
            fields.STATE: "MI",
            fields.ZIP: "48867",
        }
    )
    a2 = _account(
        **{
            fields.ACCOUNT_ID: "O2",
            fields.NAME: "Bellhaven of Owosso",
            fields.STREET: "1120 West Main Street",
            fields.CITY: "Owosso",
            fields.STATE: "MI",
            fields.ZIP: "48867",
        }
    )
    neighbor = _account(
        **{
            fields.ACCOUNT_ID: "O3",
            fields.NAME: "Other Place on Main",
            fields.STREET: "1200 West Main Street",
            fields.CITY: "Owosso",
            fields.STATE: "MI",
            fields.ZIP: "48867",
        }
    )

    groups = find_duplicates([a1, a2, neighbor])
    assert len(groups) == 1
    ids = {a[fields.ACCOUNT_ID] for a in groups[0].accounts}
    assert ids == {"O1", "O2"}


def test_carlisle_pa_never_matches_new_carlisle_oh():
    # Hard state gate. Name similarity alone must never bridge this.
    ohio = _facility(
        name="Bellhaven of New Carlisle",
        street="875 Elm St",
        city="New Carlisle",
        state="OH",
        zip="45344",
        url="https://example.test/communities/new-carlisle",
    )
    pa = _account(
        **{
            fields.ACCOUNT_ID: "PA1",
            fields.NAME: "Bellhaven of Carlisle",
            fields.STREET: "875 Elm Street",
            fields.CITY: "Carlisle",
            fields.STATE: "PA",
            fields.ZIP: "17013",
        }
    )

    results = match_facilities([ohio], [pa])
    assert results[0].account is None
    assert results[0].confidence == "none"


def test_name_only_similarity_never_matches():
    facility = _facility(name="Bellhaven of Tiffin", city="Tiffin", state="OH")
    account = _account(
        **{
            fields.ACCOUNT_ID: "T1",
            fields.NAME: "Bellhaven of Tiffin",
            fields.CITY: "Toledo",  # different city, no street
            fields.STATE: "OH",
        }
    )

    results = match_facilities([facility], [account])
    assert results[0].account is None


def test_ambiguity_margin_produces_review_item_not_action():
    facility = _facility(
        name="Bellhaven Care Center",
        street="100 Main St",
        city="Dayton",
        state="OH",
        zip="45402",
    )
    a1 = _account(
        **{
            fields.ACCOUNT_ID: "D1",
            fields.NAME: "Bellhaven Care Center East",
            fields.STREET: "100 Main Street",
            fields.CITY: "Dayton",
            fields.STATE: "OH",
            fields.ZIP: "45402",
        }
    )
    a2 = _account(
        **{
            fields.ACCOUNT_ID: "D2",
            fields.NAME: "Bellhaven Care Center West",
            fields.STREET: "100 Main Street",
            fields.CITY: "Dayton",
            fields.STATE: "OH",
            fields.ZIP: "45402",
        }
    )

    results = match_facilities([facility], [a1, a2])
    assert results[0].ambiguous is True
    assert results[0].account is None
    assert results[0].confidence == "ambiguous"
    assert len(results[0].runners_up) >= 2


def test_assignment_is_one_to_one():
    f1 = _facility(
        name="Alpha",
        street="1 Oak St",
        city="Akron",
        state="OH",
        zip="44301",
        url="https://example.test/a",
    )
    f2 = _facility(
        name="Beta",
        street="1 Oak St",
        city="Akron",
        state="OH",
        zip="44301",
        url="https://example.test/b",
    )
    account = _account(
        **{
            fields.ACCOUNT_ID: "ONLY",
            fields.NAME: "Alpha",
            fields.STREET: "1 Oak Street",
            fields.CITY: "Akron",
            fields.STATE: "OH",
            fields.ZIP: "44301",
        }
    )

    results = match_facilities([f1, f2], [account])
    matched = [r for r in results if r.account is not None]
    assert len(matched) == 1
    used_ids = [r.account[fields.ACCOUNT_ID] for r in matched]
    assert used_ids == ["ONLY"]


def test_scarce_account_prefers_global_cardinality():
    """Facility with an alternative must not consume a scarce account.

    F1 can take A at tier 1 or B at tier 2. F2 can take only A at tier 1.
    Greedy F1→A leaves F2 unmatched; global assignment yields F2→A and F1→B.
    """
    account_a = _account(
        **{
            fields.ACCOUNT_ID: "A",
            fields.NAME: "Alpha Oaks",
            fields.STREET: "100 Oak Street",
            fields.CITY: "Akron",
            fields.STATE: "OH",
            fields.ZIP: "44301",
        }
    )
    # Distinct name so F1's tier-1 vs tier-2 pair is not thin-margin ambiguous.
    account_b = _account(
        **{
            fields.ACCOUNT_ID: "B",
            fields.NAME: "Maple Ridge Manor",
            fields.STREET: "100 Oak Street",
            fields.CITY: "Akron",
            fields.STATE: "OH",
            fields.ZIP: "44302",
        }
    )
    # Same street+ZIP as A (tier 1); same street+city as B with ZIP mismatch (tier 2).
    f1 = _facility(
        name="Alpha Oaks",
        street="100 Oak St",
        city="Akron",
        state="OH",
        zip="44301",
        url="https://example.test/f1",
    )
    # Same street+ZIP as A (tier 1). Different city from B so tier 2 does not fire.
    f2 = _facility(
        name="Dayton Alpha",
        street="100 Oak St",
        city="Dayton",
        state="OH",
        zip="44301",
        url="https://example.test/f2",
    )

    results = {
        r.facility.url: r for r in match_facilities([f1, f2], [account_a, account_b])
    }

    assert results["https://example.test/f2"].account[fields.ACCOUNT_ID] == "A"
    assert results["https://example.test/f2"].tier == TIER_STREET_ZIP
    assert results["https://example.test/f1"].account[fields.ACCOUNT_ID] == "B"
    assert results["https://example.test/f1"].tier == 2
    matched = [r for r in results.values() if r.account is not None]
    assert len(matched) == 2
    assert {r.account[fields.ACCOUNT_ID] for r in matched} == {"A", "B"}


def test_intrinsic_ambiguity_not_resolved_by_other_assignments():
    """Ambiguity is fixed from the full candidate list, not leftover accounts.

    F scores A at tier 1 and B at tier 2 with nearly identical name similarity,
    so it is intrinsically ambiguous. G has a unique tier-1 claim on A only.
    Consuming A for G must not auto-assign F to B.
    """
    account_a = _account(
        **{
            fields.ACCOUNT_ID: "A",
            fields.NAME: "Alpha Oaks",
            fields.STREET: "100 Oak Street",
            fields.CITY: "Akron",
            fields.STATE: "OH",
            fields.ZIP: "44301",
        }
    )
    account_b = _account(
        **{
            fields.ACCOUNT_ID: "B",
            fields.NAME: "Alpha Oaks",
            fields.STREET: "100 Oak Street",
            fields.CITY: "Akron",
            fields.STATE: "OH",
            fields.ZIP: "44302",
        }
    )
    ambiguous = _facility(
        name="Alpha Oaks",
        street="100 Oak St",
        city="Akron",
        state="OH",
        zip="44301",
        url="https://example.test/amb",
    )
    # Same street+ZIP as A (tier 1). Different city from B so B is not a candidate.
    other = _facility(
        name="Dayton Alpha Oaks",
        street="100 Oak St",
        city="Dayton",
        state="OH",
        zip="44301",
        url="https://example.test/other",
    )

    results = {
        r.facility.url: r
        for r in match_facilities([ambiguous, other], [account_a, account_b])
    }
    assert results["https://example.test/amb"].ambiguous is True
    assert results["https://example.test/amb"].account is None
    assert results["https://example.test/amb"].confidence == "ambiguous"
    assert results["https://example.test/other"].account[fields.ACCOUNT_ID] == "A"
    # B remains unused; the optimizer must not "solve" ambiguity by assigning it.
    assert results["https://example.test/amb"].account is None
    assert all(
        r.account is None or r.account[fields.ACCOUNT_ID] != "B" for r in results.values()
    )


def test_duplicate_grouping_by_street_zip():
    a1 = _account(
        **{
            fields.ACCOUNT_ID: "O1",
            fields.NAME: "Bellhaven of Owosso",
            fields.STREET: "1120 W Main St",
            fields.CITY: "Owosso",
            fields.STATE: "MI",
            fields.ZIP: "48867",
        }
    )
    a2 = _account(
        **{
            fields.ACCOUNT_ID: "O2",
            fields.NAME: "Bellhaven of Owosso",
            fields.STREET: "1120 West Main Street",
            fields.CITY: "Owosso",
            fields.STATE: "MI",
            fields.ZIP: "48867",
        }
    )
    unique = _account(
        **{
            fields.ACCOUNT_ID: "U1",
            fields.NAME: "Bellhaven of Saline",
            fields.STREET: "980 West Michigan Avenue",
            fields.CITY: "Saline",
            fields.STATE: "MI",
            fields.ZIP: "48176",
        }
    )

    groups = find_duplicates([a1, a2, unique])
    assert len(groups) == 1
    ids = {a[fields.ACCOUNT_ID] for a in groups[0].accounts}
    assert ids == {"O1", "O2"}


def test_parent_resolves_when_exactly_one_match():
    parent = _account(
        **{
            fields.ACCOUNT_ID: "0015QAPLGS3FVYEEEM",
            fields.NAME: "Bellhaven Senior Living (Parent Account)",
            fields.PARENT_ID: "",
        }
    )
    child = _account(
        **{
            fields.ACCOUNT_ID: "C1",
            fields.NAME: "Bellhaven of Tiffin",
            fields.PARENT_ID: "0015QAPLGS3FVYEEEM",
        }
    )
    other = _account(
        **{
            fields.ACCOUNT_ID: "P2",
            fields.NAME: "Harborview Care Group (Parent Account)",
            fields.PARENT_ID: "",
        }
    )

    result = resolve_parent([parent, child, other])
    assert result.resolved
    assert result.account_id == "0015QAPLGS3FVYEEEM"
    assert result.blocker is None


def test_parent_stops_when_multiple_plausible():
    p1 = _account(
        **{
            fields.ACCOUNT_ID: "P1",
            fields.NAME: "Bellhaven Senior Living (Parent Account)",
            fields.PARENT_ID: "",
        }
    )
    p2 = _account(
        **{
            fields.ACCOUNT_ID: "P2",
            fields.NAME: "Bellhaven Senior Living",
            fields.PARENT_ID: "",
        }
    )

    result = resolve_parent([p1, p2])
    assert not result.resolved
    assert result.blocker and "Multiple plausible" in result.blocker
    assert len(result.candidates) == 2


def test_parent_override_wins():
    p1 = _account(**{fields.ACCOUNT_ID: "P1", fields.NAME: "Bellhaven Senior Living (Parent Account)"})
    result = resolve_parent([p1], override_id="P1")
    assert result.account_id == "P1"


def test_parent_never_uses_child_count_as_tiebreaker():
    # Two root "bellhaven" names: resolution must stop, even if one has more kids.
    big = _account(**{fields.ACCOUNT_ID: "BIG", fields.NAME: "Bellhaven Senior Living (Parent Account)"})
    small = _account(**{fields.ACCOUNT_ID: "SMALL", fields.NAME: "Bellhaven Senior Living"})
    kids = [
        _account(**{fields.ACCOUNT_ID: f"K{i}", fields.NAME: f"Kid {i}", fields.PARENT_ID: "BIG"})
        for i in range(5)
    ]
    result = resolve_parent([big, small, *kids])
    assert result.blocker is not None
    assert result.account_id is None


def test_assignment_reranks_after_best_account_is_consumed():
    """Stale tier-1 queue priority must not steal a tier-2 claim.

    Setup:
    - Account A and B share house number 100 in Akron but differ on street/ZIP.
    - F_taker takes A at tier 1 (exact name).
    - F_fallback initially prefers A at tier 1, with B only at tier 3.
    - F_tier2 has a clean tier-2 claim to B.

    After A is consumed, F_fallback's live best is tier 3 on B. A static queue
    would still process F_fallback ahead of F_tier2 and wrongly hand B to the
    weaker fallback. Re-ranking must give B to F_tier2.
    """
    account_a = _account(
        **{
            fields.ACCOUNT_ID: "A",
            fields.NAME: "Alpha Oaks",
            fields.STREET: "100 Oak Street",
            fields.CITY: "Akron",
            fields.STATE: "OH",
            fields.ZIP: "44301",
        }
    )
    account_b = _account(
        **{
            fields.ACCOUNT_ID: "B",
            fields.NAME: "Bellhaven Beta Gardens",
            fields.STREET: "100 Maple Avenue",
            fields.CITY: "Akron",
            fields.STATE: "OH",
            fields.ZIP: "44302",
        }
    )
    f_taker = _facility(
        name="Alpha Oaks",
        street="100 Oak St",
        city="Akron",
        state="OH",
        zip="44301",
        url="https://example.test/taker",
    )
    f_fallback = _facility(
        name="Bellhaven Beta Gardens",
        street="100 Oak Street",
        city="Akron",
        state="OH",
        zip="44301",
        url="https://example.test/fallback",
    )
    f_tier2 = _facility(
        name="Maple Spot",
        street="100 Maple Ave",
        city="Akron",
        state="OH",
        zip="44399",
        url="https://example.test/tier2",
    )

    results = {
        r.facility.url: r
        for r in match_facilities([f_fallback, f_tier2, f_taker], [account_a, account_b])
    }

    assert results["https://example.test/taker"].account["account_id"] == "A"
    assert results["https://example.test/tier2"].account["account_id"] == "B"
    assert results["https://example.test/tier2"].tier == 2
    # F_fallback's only remaining claim was the weaker tier-3 on B; it must not win.
    assert results["https://example.test/fallback"].account is None


def test_higher_name_similarity_beats_earlier_facility_order():
    """Exact name match must beat a near match listed earlier at the same tier.

    With the old packed integer cost, facility_idx * 1000 could outweigh a
    similarity gap of ~0.02 once enough earlier facilities preceded the exact
    match. Lexicographic costs keep similarity strictly above index order.
    """
    from difflib import SequenceMatcher

    from bellhaven_sync.normalize import normalize_name

    account = _account(
        **{
            fields.ACCOUNT_ID: "A",
            fields.NAME: "Alpha Oaks",
            fields.STREET: "100 Oak Street",
            fields.CITY: "Akron",
            fields.STATE: "OH",
            fields.ZIP: "44301",
        }
    )
    near_name = "Alpha Oakes"
    exact_name = "Alpha Oaks"
    near_sim = SequenceMatcher(
        None, normalize_name(near_name), normalize_name(exact_name)
    ).ratio()
    assert 0.94 <= near_sim < 1.0

    # Enough earlier near-matches that idx*1000 would exceed the similarity gap
    # under the previous packed encoding (~20k for a 0.02 gap).
    near_facilities = [
        _facility(
            name=near_name,
            street="100 Oak St",
            city="Akron",
            state="OH",
            zip="44301",
            url=f"https://example.test/near-{i}",
        )
        for i in range(25)
    ]
    exact = _facility(
        name=exact_name,
        street="100 Oak St",
        city="Akron",
        state="OH",
        zip="44301",
        url="https://example.test/exact",
    )

    for facilities in (near_facilities + [exact], [exact] + near_facilities):
        results = match_facilities(facilities, [account])
        matched = [r for r in results if r.account is not None]
        assert len(matched) == 1
        assert matched[0].facility.url == "https://example.test/exact"
        assert matched[0].name_similarity == 1.0


def test_edge_cost_similarity_dominates_index_tiebreak():
    """Any similarity improvement must beat any facility/account index pair."""
    from bellhaven_sync.matching import MatchCandidate, _edge_cost

    better = MatchCandidate("A", tier=1, name_similarity=1.0, reasons=("x",))
    worse = MatchCandidate("A", tier=1, name_similarity=0.98, reasons=("x",))
    # Extreme indices that previously could outweigh a 0.02 similarity gap.
    assert _edge_cost(better, facility_idx=10_000, account_rank=10_000) < _edge_cost(
        worse, facility_idx=0, account_rank=0
    )
    same_sim_earlier = _edge_cost(better, facility_idx=0, account_rank=0)
    same_sim_later = _edge_cost(better, facility_idx=1, account_rank=0)
    assert same_sim_earlier < same_sim_later
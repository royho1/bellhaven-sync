"""Match website facilities to CRM accounts.

Address and location evidence come first. A name-only agreement never produces
a match. State is a hard gate, which is what keeps Carlisle, PA away from
New Carlisle, OH. The scorer is a short tier list, not a weighted model, so it
can be read aloud in one minute during a walkthrough.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable

from . import fields
from .normalize import NormalizedLocation, normalize_location, normalize_name
from .scraper import Facility

NAME_SIMILARITY_FLOOR = 0.90
AMBIGUITY_MARGIN = 0.05
BELLHAVEN_PARENT_NORMALIZED = "bellhaven"

# Tier rank: lower number is better.
TIER_STREET_ZIP = 1
TIER_STREET_CITY = 2
TIER_CITY_HOUSE_NAME = 3


@dataclass(frozen=True)
class MatchCandidate:
    account_id: str
    tier: int
    name_similarity: float
    reasons: tuple[str, ...]


@dataclass
class MatchResult:
    facility: Facility
    facility_norm: NormalizedLocation
    account: dict[str, Any] | None = None
    account_norm: NormalizedLocation | None = None
    tier: int | None = None
    confidence: str = "none"
    name_similarity: float = 0.0
    reasons: list[str] = field(default_factory=list)
    ambiguous: bool = False
    runners_up: list[MatchCandidate] = field(default_factory=list)


@dataclass
class ParentResolution:
    account_id: str | None
    candidates: list[dict[str, Any]]
    blocker: str | None = None

    @property
    def resolved(self) -> bool:
        return self.account_id is not None and self.blocker is None


@dataclass
class DuplicateGroup:
    key: str
    reason: str
    accounts: list[dict[str, Any]]


def account_location(account: dict[str, Any]) -> NormalizedLocation:
    return normalize_location(
        name=account.get(fields.NAME),
        street=account.get(fields.STREET),
        city=account.get(fields.CITY),
        state=account.get(fields.STATE),
        zip_code=account.get(fields.ZIP),
    )


def facility_location(facility: Facility) -> NormalizedLocation:
    return normalize_location(
        name=facility.name,
        street=facility.street,
        city=facility.city,
        state=facility.state,
        zip_code=facility.zip,
    )


def name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _score_pair(facility: NormalizedLocation, account: NormalizedLocation) -> MatchCandidate | None:
    """Score one facility/account pair. Returns None when no tier fires."""
    if not facility.state or not account.state or facility.state != account.state:
        return None

    sim = name_similarity(facility.name, account.name)
    account_id = ""  # filled by caller

    if (
        facility.street
        and account.street
        and facility.street == account.street
        and facility.house_number
        and account.house_number
        and facility.house_number == account.house_number
    ):
        if facility.zip5 and account.zip5 and facility.zip5 == account.zip5:
            return MatchCandidate(
                account_id=account_id,
                tier=TIER_STREET_ZIP,
                name_similarity=sim,
                reasons=("same state", "same house number", "same street", "same ZIP"),
            )
        if facility.city and account.city and facility.city == account.city:
            return MatchCandidate(
                account_id=account_id,
                tier=TIER_STREET_CITY,
                name_similarity=sim,
                reasons=("same state", "same house number", "same street", "same city", "ZIP differs"),
            )

    if (
        facility.city
        and account.city
        and facility.city == account.city
        and facility.house_number
        and account.house_number
        and facility.house_number == account.house_number
        and sim >= NAME_SIMILARITY_FLOOR
    ):
        return MatchCandidate(
            account_id=account_id,
            tier=TIER_CITY_HOUSE_NAME,
            name_similarity=sim,
            reasons=(
                "same state",
                "same city",
                "same house number",
                f"name similarity {sim:.2f}",
            ),
        )

    return None


def _confidence_for_tier(tier: int) -> str:
    if tier in (TIER_STREET_ZIP, TIER_STREET_CITY):
        return "high"
    if tier == TIER_CITY_HOUSE_NAME:
        return "medium"
    return "none"


def _is_intrinsically_ambiguous(
    candidates: list[MatchCandidate],
) -> tuple[bool, list[MatchCandidate]]:
    """Ambiguity is evaluated on the facility's full candidate list.

    Consuming one candidate elsewhere must not quietly resolve the ambiguity.
    """
    if len(candidates) < 2:
        return False, []
    best, second = candidates[0], candidates[1]
    same_tier = second.tier == best.tier
    thin_margin = abs(best.name_similarity - second.name_similarity) < AMBIGUITY_MARGIN
    if same_tier or thin_margin:
        return True, candidates[1:4]
    return False, []


# Lexicographic edge cost: (tier, similarity penalty, facility_idx, account_rank).
# Tuple ordering guarantees a better tier or higher similarity always beats any
# amount of deterministic index tie-breaking, independent of graph size.
EdgeCost = tuple[int, int, int, int]
_COST_ZERO: EdgeCost = (0, 0, 0, 0)
_COST_INF: EdgeCost = (10**18, 0, 0, 0)


def _edge_cost(candidate: MatchCandidate, facility_idx: int, account_rank: int) -> EdgeCost:
    """Lower is better: tier, then higher name similarity, then stable ids."""
    similarity_penalty = int(round((1.0 - candidate.name_similarity) * 1_000_000))
    return (candidate.tier, similarity_penalty, facility_idx, account_rank)


def _add_cost(left: EdgeCost, right: EdgeCost) -> EdgeCost:
    return (left[0] + right[0], left[1] + right[1], left[2] + right[2], left[3] + right[3])


def _sub_cost(left: EdgeCost, right: EdgeCost) -> EdgeCost:
    return (left[0] - right[0], left[1] - right[1], left[2] - right[2], left[3] - right[3])


def _min_cost_max_matching(
    facility_indices: list[int],
    candidates_by_idx: dict[int, list[MatchCandidate]],
) -> dict[int, MatchCandidate]:
    """Max-cardinality bipartite matching with min total edge cost.

    Successive shortest augmenting paths. Prefer more matches first, then lower
    tiers / higher similarities via lexicographic edge costs. Deterministic.
    """
    f_list = sorted(facility_indices)
    if not f_list:
        return {}

    account_ids = sorted(
        {c.account_id for fi in f_list for c in candidates_by_idx.get(fi, [])}
    )
    if not account_ids:
        return {}

    a_rank = {aid: i for i, aid in enumerate(account_ids)}
    edges: dict[tuple[int, str], tuple[EdgeCost, MatchCandidate]] = {}
    for fi in f_list:
        for candidate in candidates_by_idx[fi]:
            edges[(fi, candidate.account_id)] = (
                _edge_cost(candidate, fi, a_rank[candidate.account_id]),
                candidate,
            )

    match_fac: dict[int, str] = {}
    match_acc: dict[str, int] = {}
    chosen: dict[int, MatchCandidate] = {}

    def augment() -> bool:
        dist_f: dict[int, EdgeCost] = {fi: _COST_INF for fi in f_list}
        dist_a: dict[str, EdgeCost] = {aid: _COST_INF for aid in account_ids}
        parent_a: dict[str, int] = {}
        parent_f: dict[int, str] = {}
        queue: list[tuple[str, int | str]] = []
        in_f: set[int] = set()
        in_a: set[str] = set()

        for fi in f_list:
            if fi not in match_fac:
                dist_f[fi] = _COST_ZERO
                queue.append(("f", fi))
                in_f.add(fi)

        best_free_acc: str | None = None
        best_free_dist: EdgeCost = _COST_INF

        while queue:
            kind, node = queue.pop(0)
            if kind == "f":
                fi = int(node)
                in_f.discard(fi)
                for aid in account_ids:
                    key = (fi, aid)
                    if key not in edges:
                        continue
                    if match_fac.get(fi) == aid:
                        continue
                    cost, _ = edges[key]
                    nd = _add_cost(dist_f[fi], cost)
                    if nd < dist_a[aid]:
                        dist_a[aid] = nd
                        parent_a[aid] = fi
                        if aid not in match_acc and (
                            nd < best_free_dist
                            or (
                                nd == best_free_dist
                                and (best_free_acc is None or aid < best_free_acc)
                            )
                        ):
                            best_free_dist = nd
                            best_free_acc = aid
                        if aid not in in_a:
                            queue.append(("a", aid))
                            in_a.add(aid)
            else:
                aid = str(node)
                in_a.discard(aid)
                if aid not in match_acc:
                    continue
                fi = match_acc[aid]
                cost, _ = edges[(fi, aid)]
                nd = _sub_cost(dist_a[aid], cost)
                if nd < dist_f[fi]:
                    dist_f[fi] = nd
                    parent_f[fi] = aid
                    if fi not in in_f:
                        queue.append(("f", fi))
                        in_f.add(fi)

        if best_free_acc is None:
            return False

        aid: str | None = best_free_acc
        while aid is not None:
            fi = parent_a[aid]
            prior = match_fac.get(fi)
            match_fac[fi] = aid
            match_acc[aid] = fi
            chosen[fi] = edges[(fi, aid)][1]
            if prior is not None and match_acc.get(prior) == fi:
                del match_acc[prior]
            if fi not in parent_f:
                break
            aid = parent_f[fi]
        return True

    while augment():
        pass

    return chosen


def match_facilities(
    facilities: Iterable[Facility],
    accounts: Iterable[dict[str, Any]],
) -> list[MatchResult]:
    """One-to-one assignment that protects scarce accounts and ambiguity.

    1. Facilities that are intrinsically ambiguous on their full candidate list
       become human-review items and never receive an automatic account.
    2. Remaining facilities are assigned with a max-cardinality, min-cost
       bipartite matching so a facility with alternatives does not consume the
       only account another facility can use.
    """
    facility_list = list(facilities)
    account_list = list(accounts)
    account_norms = {a.get(fields.ACCOUNT_ID): account_location(a) for a in account_list}
    accounts_by_id = {a.get(fields.ACCOUNT_ID): a for a in account_list}

    per_facility: list[tuple[Facility, NormalizedLocation, list[MatchCandidate]]] = []
    for facility in facility_list:
        fnorm = facility_location(facility)
        candidates: list[MatchCandidate] = []
        for account in account_list:
            anorm = account_norms[account.get(fields.ACCOUNT_ID)]
            scored = _score_pair(fnorm, anorm)
            if scored is None:
                continue
            candidates.append(
                MatchCandidate(
                    account_id=str(account.get(fields.ACCOUNT_ID)),
                    tier=scored.tier,
                    name_similarity=scored.name_similarity,
                    reasons=scored.reasons,
                )
            )
        candidates.sort(key=lambda c: (c.tier, -c.name_similarity, c.account_id))
        per_facility.append((facility, fnorm, candidates))

    assigned: dict[int, MatchResult] = {}
    assignable: list[int] = []
    candidates_by_idx: dict[int, list[MatchCandidate]] = {}

    for idx, (facility, fnorm, candidates) in enumerate(per_facility):
        if not candidates:
            continue
        ambiguous, runners = _is_intrinsically_ambiguous(candidates)
        if ambiguous:
            best = candidates[0]
            assigned[idx] = MatchResult(
                facility=facility,
                facility_norm=fnorm,
                account=None,
                tier=best.tier,
                confidence="ambiguous",
                name_similarity=best.name_similarity,
                reasons=list(best.reasons) + ["ambiguous: multiple candidates"],
                ambiguous=True,
                runners_up=[best, *runners],
            )
            continue
        assignable.append(idx)
        candidates_by_idx[idx] = candidates

    chosen = _min_cost_max_matching(assignable, candidates_by_idx)
    for idx, candidate in chosen.items():
        facility, fnorm, _ = per_facility[idx]
        account = accounts_by_id[candidate.account_id]
        assigned[idx] = MatchResult(
            facility=facility,
            facility_norm=fnorm,
            account=account,
            account_norm=account_norms[candidate.account_id],
            tier=candidate.tier,
            confidence=_confidence_for_tier(candidate.tier),
            name_similarity=candidate.name_similarity,
            reasons=list(candidate.reasons),
        )

    results: list[MatchResult] = []
    for idx, (facility, fnorm, _) in enumerate(per_facility):
        if idx in assigned:
            results.append(assigned[idx])
        else:
            results.append(
                MatchResult(
                    facility=facility,
                    facility_norm=fnorm,
                    reasons=["no tier matched"],
                )
            )
    return results


def find_duplicates(accounts: Iterable[dict[str, Any]]) -> list[DuplicateGroup]:
    """Group CRM accounts that look like the same facility. No AR policy here."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    name_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for account in accounts:
        norm = account_location(account)
        if not norm.state:
            continue
        if norm.street and norm.zip5 and norm.house_number:
            key = ("street_zip", f"{norm.state}|{norm.house_number}|{norm.street}|{norm.zip5}")
            groups.setdefault(key, []).append(account)
        if norm.city and norm.name:
            key = ("city_name", f"{norm.state}|{norm.city}|{norm.name}")
            name_groups.setdefault(key, []).append(account)

    duplicates: list[DuplicateGroup] = []
    seen_ids: set[frozenset[str]] = set()

    for (kind, key), members in list(groups.items()) + list(name_groups.items()):
        if len(members) < 2:
            continue
        ids = frozenset(str(m.get(fields.ACCOUNT_ID)) for m in members)
        if ids in seen_ids:
            continue
        seen_ids.add(ids)
        reason = (
            "same state, house number, street, and ZIP"
            if kind == "street_zip"
            else "same state, city, and normalized name"
        )
        duplicates.append(DuplicateGroup(key=key, reason=reason, accounts=members))

    return duplicates


def resolve_parent(
    accounts: Iterable[dict[str, Any]],
    *,
    override_id: str | None = None,
) -> ParentResolution:
    """Find the Bellhaven parent. Never break a tie by counting children."""
    account_list = list(accounts)
    by_id = {str(a.get(fields.ACCOUNT_ID)): a for a in account_list}

    if override_id:
        if override_id in by_id:
            return ParentResolution(account_id=override_id, candidates=[by_id[override_id]])
        return ParentResolution(
            account_id=None,
            candidates=[],
            blocker=(
                f"BELLHAVEN_PARENT_ACCOUNT_ID={override_id!r} is set but no account "
                "with that id exists in the CRM."
            ),
        )

    # After filler stripping, "Bellhaven Senior Living (Parent Account)" -> "bellhaven".
    # Exact brand match only. Child count is never a tiebreaker.
    candidates = [
        account
        for account in account_list
        if normalize_name(account.get(fields.NAME)) == BELLHAVEN_PARENT_NORMALIZED
        and not fields.is_set(account.get(fields.PARENT_ID))
    ]

    if len(candidates) == 1:
        return ParentResolution(account_id=str(candidates[0].get(fields.ACCOUNT_ID)), candidates=candidates)

    if not candidates:
        return ParentResolution(
            account_id=None,
            candidates=[],
            blocker=(
                "No Bellhaven parent account found. Looking for a root account "
                "whose normalized name is 'bellhaven' (e.g. 'Bellhaven Senior Living "
                "(Parent Account)'). Set BELLHAVEN_PARENT_ACCOUNT_ID to override."
            ),
        )

    summary = ", ".join(
        f"{a.get(fields.ACCOUNT_ID)}:{a.get(fields.NAME)!r}" for a in candidates
    )
    return ParentResolution(
        account_id=None,
        candidates=candidates,
        blocker=(
            f"Multiple plausible Bellhaven parent accounts: {summary}. "
            "Refusing to guess. Set BELLHAVEN_PARENT_ACCOUNT_ID after human confirmation."
        ),
    )

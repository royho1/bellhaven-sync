"""Change-of-ownership (CHOW) decision policy.

When an existing facility must move to a different parent:

* revenue history AND outstanding_ar > 0 → two-step CHOW plan
  (create new account under the correct parent; later PATCH old account with
  only ``chow_current_account``)
* otherwise → re-parent the existing account directly

Revenue/AR logic applies only to ownership/parent changes. The ambiguous case
``lifetime_revenue == 0`` with ``outstanding_ar > 0`` is never auto-decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import fields

# Until product clarifies whether lifetime_revenue of exactly zero means
# "no revenue history", positive AR with zero revenue routes to human review.
ZERO_LIFETIME_REVENUE_MEANS_NO_HISTORY = True

KIND_REPARENT = "reparent"
KIND_CHOW_TWO_STEP = "chow_two_step"
KIND_HUMAN_REVIEW = "human_review"


@dataclass(frozen=True)
class ChowDecision:
    kind: str
    reason: str
    lifetime_revenue: int
    outstanding_ar: int
    target_parent_id: str

    @property
    def needs_human_review(self) -> bool:
        return self.kind == KIND_HUMAN_REVIEW


def _as_nonneg_int(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def decide_parent_change(
    account: dict[str, Any],
    *,
    target_parent_id: str,
) -> ChowDecision:
    """Decide how to move ``account`` under ``target_parent_id``."""
    revenue = _as_nonneg_int(account.get(fields.LIFETIME_REVENUE))
    ar = _as_nonneg_int(account.get(fields.OUTSTANDING_AR))

    if ar > 0 and revenue == 0:
        return ChowDecision(
            kind=KIND_HUMAN_REVIEW,
            reason=(
                "outstanding_ar > 0 with lifetime_revenue == 0 is an ambiguous "
                "revenue-history signal; do not auto-choose re-parent vs CHOW"
            ),
            lifetime_revenue=revenue,
            outstanding_ar=ar,
            target_parent_id=target_parent_id,
        )

    if revenue > 0 and ar > 0:
        return ChowDecision(
            kind=KIND_CHOW_TWO_STEP,
            reason=(
                "account has revenue history and outstanding_ar > 0; create a new "
                "account under the correct parent, then link the old account via "
                "chow_current_account only"
            ),
            lifetime_revenue=revenue,
            outstanding_ar=ar,
            target_parent_id=target_parent_id,
        )

    return ChowDecision(
        kind=KIND_REPARENT,
        reason=(
            "no blocking AR with revenue history; re-parent the existing account "
            "directly under the correct parent"
        ),
        lifetime_revenue=revenue,
        outstanding_ar=ar,
        target_parent_id=target_parent_id,
    )

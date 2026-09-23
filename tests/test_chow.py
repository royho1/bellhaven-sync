"""Focused tests for the CHOW parent-change decision policy."""

from __future__ import annotations

from bellhaven_sync import fields
from bellhaven_sync.chow import (
    KIND_CHOW_TWO_STEP,
    KIND_HUMAN_REVIEW,
    KIND_REPARENT,
    decide_parent_change,
)


def _account(**kwargs):
    base = {
        fields.ACCOUNT_ID: "A1",
        fields.NAME: "Facility",
        fields.PARENT_ID: "OLD",
        fields.LIFETIME_REVENUE: 0,
        fields.OUTSTANDING_AR: 0,
    }
    base.update(kwargs)
    return base


def test_reparent_when_no_blocking_ar():
    decision = decide_parent_change(_account(lifetime_revenue=0, outstanding_ar=0), target_parent_id="P")
    assert decision.kind == KIND_REPARENT
    assert decision.target_parent_id == "P"


def test_reparent_when_revenue_but_zero_ar():
    decision = decide_parent_change(
        _account(lifetime_revenue=5000, outstanding_ar=0),
        target_parent_id="P",
    )
    assert decision.kind == KIND_REPARENT


def test_chow_two_step_when_revenue_and_positive_ar():
    decision = decide_parent_change(
        _account(lifetime_revenue=12000, outstanding_ar=400),
        target_parent_id="P",
    )
    assert decision.kind == KIND_CHOW_TWO_STEP
    assert "chow_current_account" in decision.reason


def test_human_review_when_zero_revenue_and_positive_ar():
    decision = decide_parent_change(
        _account(lifetime_revenue=0, outstanding_ar=250),
        target_parent_id="P",
    )
    assert decision.kind == KIND_HUMAN_REVIEW
    assert decision.needs_human_review

from __future__ import annotations

import json

from bellhaven_sync import schema_probe

from .conftest import FAKE_TOKEN

ACCOUNTS = [
    {
        "id": 1,
        "name": "Bellhaven Senior Living",
        "parent_id": None,
        "status": "active",
        "note": None,
        "lifetime_revenue": 0,
        "outstanding_ar": 0,
        "chow_current_account": None,
        "duplicate_of_account": None,
    },
    {
        "id": 2,
        "name": "Bellhaven Meadows of Findlay",
        "parent_id": 1,
        "status": "active",
        "note": "verified by ops",
        "lifetime_revenue": "1,250.50",
        "outstanding_ar": 400.0,
        "chow_current_account": None,
        "duplicate_of_account": None,
    },
    {
        "id": 3,
        "name": "Harborview Care Group",
        "parent_id": None,
        "status": "inactive",
        "note": None,
        "lifetime_revenue": None,
        "outstanding_ar": -25,
        "chow_current_account": 2,
        "duplicate_of_account": None,
    },
]


def test_envelope_description_for_an_object_payload():
    envelope = schema_probe.describe_envelope({"items": [{"id": 1}], "page": 1, "total": 3})

    assert envelope["kind"] == "object"
    assert envelope["item_key"] == "items"
    assert "page" in envelope["pagination_keys"]
    assert envelope["pagination_values"]["total"] == 3


def test_envelope_description_for_a_bare_list():
    envelope = schema_probe.describe_envelope([{"id": 1}])

    assert envelope["kind"] == "bare list"
    assert envelope["item_key"] is None


def test_field_summary_counts_nulls_and_types():
    report = schema_probe.analyze_accounts(ACCOUNTS)

    assert report["account_count"] == 3
    assert report["fields"]["name"]["non_null"] == 3
    assert report["fields"]["lifetime_revenue"]["types"] == {"int": 1, "str": 1, "null": 1}
    assert "name" in report["always_populated"]
    assert "note" in report["sometimes_null"]
    assert "duplicate_of_account" in report["always_null"]


def test_status_values_are_enumerated():
    report = schema_probe.analyze_accounts(ACCOUNTS)

    assert report["status_values"] == {"active": 2, "inactive": 1}


def test_hierarchy_reports_roots_without_ranking_them():
    report = schema_probe.analyze_accounts(ACCOUNTS)

    hierarchy = report["hierarchy"]
    assert hierarchy["root_count"] == 2
    assert {root["id"] for root in hierarchy["root_accounts"]} == {1, 3}
    assert hierarchy["max_depth"] == 1


def test_money_summary_separates_zero_positive_and_negative():
    report = schema_probe.analyze_accounts(ACCOUNTS)

    revenue = report["money_fields"]["lifetime_revenue"]
    assert revenue["zero"] == 1
    assert revenue["positive"] == 1
    assert revenue["null"] == 1

    ar = report["money_fields"]["outstanding_ar"]
    assert ar["negative"] == 1
    assert ar["min"] == -25


def test_money_summary_flags_unparseable_values():
    report = schema_probe.analyze_accounts([{"lifetime_revenue": "unknown", "outstanding_ar": 1}])

    assert report["money_fields"]["lifetime_revenue"]["unparseable_samples"] == ["unknown"]


def test_missing_expected_fields_are_reported():
    report = schema_probe.analyze_accounts([{"id": 1, "name": "Only Basics"}])

    assert "outstanding_ar" in report["missing_expected_fields"]
    assert "chow_current_account" in report["missing_expected_fields"]


def test_report_text_contains_no_token(settings):
    report = schema_probe.analyze_accounts(ACCOUNTS)

    text = schema_probe.format_report(report, schema_probe.describe_envelope(ACCOUNTS), {"token": FAKE_TOKEN})

    assert FAKE_TOKEN not in text
    assert "***REDACTED***" in text


def test_snapshot_is_written_to_the_data_dir_without_the_token(settings):
    path = schema_probe.write_snapshot(ACCOUNTS, settings)

    assert path.exists()
    assert path.parent == settings.snapshot_dir
    contents = path.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in contents
    assert json.loads(contents)["account_count"] == 3

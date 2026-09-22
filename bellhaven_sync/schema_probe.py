"""Phase 0: learn the real account schema by reading the API.

The OpenAPI spec documents the endpoints but leaves the account schemas as
empty objects, so field names, types, and status values have to be observed
rather than assumed. Nothing here writes to the CRM.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import crm_client
from .config import Settings, redact

# Fields the assessment talks about. Phase 0 confirms whether the API really
# calls them this, so lookups are case-insensitive and absence is reported.
FIELDS_OF_INTEREST = (
    "account_id",
    "name",
    "parent_id",
    "parent_name",
    "status",
    "note",
    "lifetime_revenue",
    "outstanding_ar",
    "chow_current_account",
    "duplicate_of_account",
)

# The identifier field, tried in order. The API calls it account_id, not id.
ID_FIELD_CANDIDATES = ("account_id", "id")

MONEY_FIELDS = ("lifetime_revenue", "outstanding_ar")

# Values printed verbatim in the report are capped so a snapshot of real
# records never turns into a wall of data in a committed document.
SAMPLE_LIMIT = 5


def describe_envelope(payload: Any) -> dict[str, Any]:
    """Describe the shape of a GET /accounts response."""
    if isinstance(payload, list):
        return {"kind": "bare list", "top_level_keys": [], "item_key": None, "pagination_keys": []}
    if not isinstance(payload, dict):
        return {"kind": type(payload).__name__, "top_level_keys": [], "item_key": None, "pagination_keys": []}

    keys = sorted(payload)
    item_key = next((k for k in crm_client.ITEM_KEYS if isinstance(payload.get(k), list)), None)
    pagination_keys = [k for k in keys if k != item_key]
    return {
        "kind": "object",
        "top_level_keys": keys,
        "item_key": item_key,
        "pagination_keys": pagination_keys,
        "pagination_values": {k: payload[k] for k in pagination_keys if not isinstance(payload[k], (dict, list))},
    }


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    return type(value).__name__


def _resolve_key(field_names: list[str], wanted: str) -> str | None:
    for name in field_names:
        if name.lower() == wanted.lower():
            return name
    return None


def is_blank(value: Any) -> bool:
    """This API uses the empty string, not null, to mean 'unset'."""
    return value is None or (isinstance(value, str) and value.strip() == "")


def _summarize_money(values: list[Any]) -> dict[str, Any]:
    """Report exactly what a money-ish field looks like, without interpreting it."""
    numeric: list[float] = []
    unparseable: list[Any] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            unparseable.append(value)
        elif isinstance(value, (int, float)):
            numeric.append(float(value))
        elif isinstance(value, str):
            cleaned = value.replace("$", "").replace(",", "").strip()
            try:
                numeric.append(float(cleaned))
            except ValueError:
                unparseable.append(value)
        else:
            unparseable.append(value)

    return {
        "present": sum(1 for v in values if v is not None),
        "null": sum(1 for v in values if v is None),
        "types": dict(Counter(_type_name(v) for v in values)),
        "zero": sum(1 for v in numeric if v == 0),
        "positive": sum(1 for v in numeric if v > 0),
        "negative": sum(1 for v in numeric if v < 0),
        "min": min(numeric) if numeric else None,
        "max": max(numeric) if numeric else None,
        "unparseable_samples": unparseable[:SAMPLE_LIMIT],
    }


def analyze_accounts(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the schema report from a list of account records."""
    total = len(accounts)
    type_counts: dict[str, Counter] = defaultdict(Counter)
    present_counts: Counter = Counter()
    non_null_counts: Counter = Counter()
    non_blank_counts: Counter = Counter()

    for account in accounts:
        for key, value in account.items():
            present_counts[key] += 1
            type_counts[key][_type_name(value)] += 1
            if value is not None:
                non_null_counts[key] += 1
            if not is_blank(value):
                non_blank_counts[key] += 1

    field_names = sorted(present_counts)
    fields = {
        name: {
            "present_in": present_counts[name],
            "non_null": non_null_counts[name],
            "non_blank": non_blank_counts[name],
            "blank": present_counts[name] - non_blank_counts[name],
            "null_rate": round(1 - (non_null_counts[name] / total), 3) if total else None,
            "types": dict(type_counts[name]),
        }
        for name in field_names
    }

    resolved = {wanted: _resolve_key(field_names, wanted) for wanted in FIELDS_OF_INTEREST}

    status_key = resolved.get("status")
    status_values = (
        dict(Counter(account.get(status_key) for account in accounts)) if status_key else {}
    )

    parent_key = resolved.get("parent_id")
    id_key = next(
        (key for key in (_resolve_key(field_names, c) for c in ID_FIELD_CANDIDATES) if key),
        None,
    )
    name_key = resolved.get("name") or "name"
    hierarchy: dict[str, Any] = {}
    if parent_key and id_key:
        parents = [account.get(parent_key) for account in accounts]
        roots = [account for account in accounts if is_blank(account.get(parent_key))]
        children_per_parent = Counter(p for p in parents if not is_blank(p))
        by_id = {account.get(id_key): account for account in accounts}
        depths = []
        for account in accounts:
            depth, cursor, guard = 0, account, 0
            while cursor is not None and not is_blank(cursor.get(parent_key)) and guard < 20:
                cursor = by_id.get(cursor.get(parent_key))
                depth += 1
                guard += 1
            depths.append(depth)
        hierarchy = {
            "parent_id_types": dict(Counter(_type_name(p) for p in parents)),
            "root_accounts": [
                {
                    "id": a.get(id_key),
                    "name": a.get(name_key),
                    "children": children_per_parent.get(a.get(id_key), 0),
                }
                for a in roots[:25]
            ],
            "root_count": len(roots),
            "distinct_parents": len(children_per_parent),
            "max_depth": max(depths) if depths else 0,
            "dangling_parent_ids": sorted(
                {p for p in parents if not is_blank(p) and p not in by_id}
            )[:SAMPLE_LIMIT],
        }

    money = {}
    for field in MONEY_FIELDS:
        key = resolved.get(field)
        if key:
            money[field] = _summarize_money([account.get(key) for account in accounts])

    link_fields = {}
    for field in ("chow_current_account", "duplicate_of_account"):
        key = resolved.get(field)
        if key:
            values = [account.get(key) for account in accounts]
            link_fields[field] = {
                "resolved_key": key,
                "set_count": sum(1 for v in values if not is_blank(v)),
                "types": dict(Counter(_type_name(v) for v in values)),
                "samples": [v for v in values if not is_blank(v)][:SAMPLE_LIMIT],
            }

    note_key = resolved.get("note")
    note_summary = None
    if note_key:
        notes = [a.get(note_key) for a in accounts]
        note_summary = {
            "resolved_key": note_key,
            "non_null": sum(1 for n in notes if not is_blank(n)),
            "types": dict(Counter(_type_name(n) for n in notes)),
            "max_length": max((len(n) for n in notes if isinstance(n, str)), default=0),
            "samples": [n for n in notes if not is_blank(n)][:SAMPLE_LIMIT],
        }

    enumerations = {}
    for field in ("care_type", "billing_state"):
        key = _resolve_key(field_names, field)
        if key:
            enumerations[key] = dict(Counter(a.get(key) for a in accounts).most_common(15))

    return {
        "account_count": total,
        "fields": fields,
        "resolved_fields": resolved,
        "missing_expected_fields": [k for k, v in resolved.items() if v is None],
        "always_populated": [name for name in field_names if non_blank_counts[name] == total],
        "sometimes_null": [name for name in field_names if 0 < non_blank_counts[name] < total],
        "always_null": [name for name in field_names if non_blank_counts[name] == 0],
        "status_values": status_values,
        "hierarchy": hierarchy,
        "money_fields": money,
        "link_fields": link_fields,
        "note": note_summary,
        "enumerations": enumerations,
    }


def format_report(report: dict[str, Any], envelope: dict[str, Any], me: Any) -> str:
    """Render the report for the terminal. Everything passes through redact()."""
    lines: list[str] = []
    add = lines.append

    add("=" * 72)
    add("bellhaven-sync schema discovery (read-only)")
    add("=" * 72)
    add("")
    add(f"GET /me -> {redact(json.dumps(me, default=str))[:400]}")
    add("")
    add("-- GET /accounts envelope --")
    add(f"  shape: {envelope['kind']}")
    add(f"  top-level keys: {envelope['top_level_keys'] or 'n/a (bare list)'}")
    add(f"  list lives under: {envelope['item_key'] or 'the response itself'}")
    if envelope.get("pagination_values"):
        add(f"  pagination values on page 1: {envelope['pagination_values']}")
    add("")
    add(f"-- accounts retrieved: {report['account_count']} --")
    add("")
    add("-- fields (name: non-blank/total, types) --")
    for name, info in report["fields"].items():
        add(f"  {name}: {info['non_blank']}/{report['account_count']}  {info['types']}")
    add("")
    add(f"-- always set:   {report['always_populated']}")
    add(f"-- sometimes set: {report['sometimes_null']}")
    add(f"-- never set:    {report['always_null']}")
    add("")
    if report.get("enumerations"):
        add("-- value distributions --")
        for key, counts in report["enumerations"].items():
            add(f"  {key}: {counts}")
        add("")
    if report["missing_expected_fields"]:
        add(f"!! expected fields NOT present in the API: {report['missing_expected_fields']}")
        add("")
    add("-- status values --")
    for value, count in sorted(report["status_values"].items(), key=lambda kv: str(kv[0])):
        add(f"  {value!r}: {count}")
    add("")
    if report["hierarchy"]:
        hierarchy = report["hierarchy"]
        add("-- hierarchy --")
        add(f"  parent_id types: {hierarchy['parent_id_types']}")
        add(f"  root accounts (no parent): {hierarchy['root_count']}")
        for root in hierarchy["root_accounts"]:
            add(f"    id={root['id']!r} name={root['name']!r} children={root['children']}")
        add(f"  distinct parents referenced: {hierarchy['distinct_parents']}")
        add(f"  max depth observed: {hierarchy['max_depth']}")
        if hierarchy.get("dangling_parent_ids"):
            add(f"  parent ids with no matching account: {hierarchy['dangling_parent_ids']}")
        add("")
    if report["money_fields"]:
        add("-- financial fields (drives the CHOW decision) --")
        for field, info in report["money_fields"].items():
            add(f"  {field}:")
            add(f"    types: {info['types']}")
            add(f"    null={info['null']} zero={info['zero']} positive={info['positive']} negative={info['negative']}")
            add(f"    range: {info['min']} .. {info['max']}")
            if info["unparseable_samples"]:
                add(f"    unparseable samples: {info['unparseable_samples']}")
        add("")
    if report["link_fields"]:
        add("-- link fields --")
        for field, info in report["link_fields"].items():
            add(f"  {field} (key {info['resolved_key']!r}): set={info['set_count']} types={info['types']}")
            if info["samples"]:
                add(f"    samples: {info['samples']}")
        add("")
    if report["note"]:
        note = report["note"]
        add(f"-- note (key {note['resolved_key']!r}): non-empty={note['non_null']} types={note['types']} max_len={note['max_length']}")
        add("")

    return redact("\n".join(lines))


def write_snapshot(accounts: list[dict[str, Any]], settings: Settings) -> Path:
    """Save the raw accounts to the gitignored data directory."""
    settings.snapshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = settings.snapshot_dir / f"accounts-{stamp}.json"
    payload = {
        "fetched_at": stamp,
        "base_url": settings.base_url,
        "account_count": len(accounts),
        "accounts": accounts,
    }
    path.write_text(redact(json.dumps(payload, indent=2, default=str)), encoding="utf-8")
    return path


def run_discovery(settings: Settings, *, page_size: int = crm_client.DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    """Call /me, page through /accounts, analyze, snapshot, and return the report."""
    me = crm_client.get_me(settings=settings)
    first_page = crm_client.get_accounts(page=1, page_size=page_size, settings=settings)
    envelope = describe_envelope(first_page)
    accounts = list(crm_client.iter_accounts(page_size=page_size, settings=settings))
    report = analyze_accounts(accounts)
    snapshot_path = write_snapshot(accounts, settings)
    return {
        "me": me,
        "envelope": envelope,
        "report": report,
        "accounts": accounts,
        "snapshot_path": snapshot_path,
        "text": format_report(report, envelope, me),
    }

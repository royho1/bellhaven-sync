"""CRM field names, confirmed against the live API in Phase 0.

The OpenAPI spec leaves the account schema empty, so every name here was
observed from real responses (see docs/schema-findings.md). Routing all field
access through this module means a schema surprise is a one-line change.
"""

from __future__ import annotations

from typing import Any

ACCOUNT_ID = "account_id"
NAME = "name"
PARENT_ID = "parent_id"
PARENT_NAME = "parent_name"
STREET = "billing_street"
CITY = "billing_city"
STATE = "billing_state"
ZIP = "billing_zip"
CARE_TYPE = "care_type"
STATUS = "status"
PHONE = "phone"
LIFETIME_REVENUE = "lifetime_revenue"
OUTSTANDING_AR = "outstanding_ar"
CHOW_CURRENT_ACCOUNT = "chow_current_account"
DUPLICATE_OF_ACCOUNT = "duplicate_of_account"
NOTE = "note"
CREATED_BY_CANDIDATE = "created_by_candidate"
UPDATED_AT = "updated_at"

ADDRESS_FIELDS = (STREET, CITY, STATE, ZIP)

STATUS_ACTIVE = "Active"
STATUS_INACTIVE = "Inactive"

# The API never returns null. Unset string fields come back as "".
UNSET = ""


def is_set(value: Any) -> bool:
    """True when a field carries a real value rather than the unset sentinel."""
    return value is not None and not (isinstance(value, str) and value.strip() == "")


def get(account: dict[str, Any], field: str, default: Any = UNSET) -> Any:
    """Read a field, treating the unset sentinel as absent."""
    value = account.get(field, default)
    return value if is_set(value) else default

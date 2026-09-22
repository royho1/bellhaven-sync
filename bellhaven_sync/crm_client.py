"""Read-only access to the Clipboard CRM API.

This module is GET-only and stays GET-only. It has no POST, PATCH, PUT, or
DELETE helper now and must never gain one: all write capability lives in
`apply.py`, which is unreachable from the scheduled sync path. `tests/
test_crm_client.py` and `tests/test_write_boundary.py` enforce both halves of
that rule.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import requests

from .config import DEFAULT_TIMEOUT, Settings, build_session, load_settings, redact

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0

# A page-based API with an unknown envelope: Phase 0 confirms which of these
# the server actually uses, and the client accepts any of them meanwhile.
ITEM_KEYS = ("items", "data", "accounts", "results", "records")

# Guards against a server that ignores paging and returns the same page forever.
MAX_PAGES = 1000


class CrmError(RuntimeError):
    """An API call failed. Messages are always redacted."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(redact(message))
        self.status_code = status_code


_session: requests.Session | None = None


def _resolve(session: requests.Session | None, settings: Settings | None) -> tuple[Any, Settings]:
    global _session
    resolved_settings = settings or load_settings()
    if session is not None:
        return session, resolved_settings
    if _session is None:
        _session = build_session(resolved_settings)
    return _session, resolved_settings


def reset_session() -> None:
    """Drop the cached session. Used by tests and after a settings reload."""
    global _session
    if _session is not None:
        _session.close()
    _session = None


def _describe_failure(status: int, body: str) -> str:
    snippet = redact(body)[:300]
    if status == 401:
        return "Authentication failed (401). Check CLIPBOARD_API_TOKEN in your .env."
    if status == 403:
        return "Not authorized (403). The token is valid but lacks access to this resource."
    if status == 404:
        return f"Not found (404): {snippet}"
    if status == 422:
        return f"Request rejected (422): {snippet}"
    if status == 429:
        return f"Rate limited (429): {snippet}"
    if status >= 500:
        return f"Server error ({status}): {snippet}"
    return f"Unexpected response ({status}): {snippet}"


def _get(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    session: requests.Session | None = None,
    settings: Settings | None = None,
) -> Any:
    """Issue a single GET and return decoded JSON.

    Retries only on connection failures and 5xx responses, which are the cases
    where retrying is safe and likely to help.
    """
    http, resolved = _resolve(session, settings)
    url = f"{resolved.base_url}/{path.lstrip('/')}"
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}

    last_error: str | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = http.get(url, params=clean_params, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            last_error = f"Could not reach {url}: {redact(exc)}"
            logger.warning("GET %s failed (attempt %s/%s): %s", url, attempt, MAX_RETRIES, last_error)
            if attempt == MAX_RETRIES:
                raise CrmError(last_error) from None
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        status = response.status_code
        if status >= 500 and attempt < MAX_RETRIES:
            logger.warning("GET %s returned %s, retrying (%s/%s)", url, status, attempt, MAX_RETRIES)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue
        if status >= 400:
            raise CrmError(_describe_failure(status, response.text or ""), status_code=status)

        try:
            return response.json()
        except ValueError:
            raise CrmError(f"Response from {url} was not JSON: {redact(response.text)[:300]}") from None

    raise CrmError(last_error or f"GET {url} failed after {MAX_RETRIES} attempts")


def extract_items(payload: Any) -> list[dict[str, Any]]:
    """Pull the account list out of whichever envelope the API returns."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ITEM_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise CrmError(
        "Could not find a list of accounts in the response. "
        f"Top-level type was {type(payload).__name__}"
        + (f" with keys {sorted(payload)[:12]}" if isinstance(payload, dict) else "")
    )


def get_me(
    *,
    session: requests.Session | None = None,
    settings: Settings | None = None,
) -> Any:
    """GET /me. Confirms the token works and shows who it belongs to."""
    return _get("/me", session=session, settings=settings)


def get_accounts(
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    session: requests.Session | None = None,
    settings: Settings | None = None,
    **filters: Any,
) -> Any:
    """GET /accounts for a single page, returning the raw payload.

    Supported filters per the OpenAPI spec: q, city, state, zip, street, parent_id.
    """
    params = {"page": page, "page_size": page_size, **filters}
    return _get("/accounts", params=params, session=session, settings=settings)


def iter_accounts(
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    session: requests.Session | None = None,
    settings: Settings | None = None,
    **filters: Any,
) -> Iterator[dict[str, Any]]:
    """Yield every account, walking pages until a short or empty page arrives."""
    seen_pages = 0
    for page in range(1, MAX_PAGES + 1):
        payload = get_accounts(
            page=page,
            page_size=page_size,
            session=session,
            settings=settings,
            **filters,
        )
        items = extract_items(payload)
        seen_pages += 1
        for item in items:
            yield item
        if len(items) < page_size:
            return
    logger.warning("Stopped paging after %s pages; the API may be ignoring the page parameter", seen_pages)


def get_account(
    account_id: Any,
    *,
    session: requests.Session | None = None,
    settings: Settings | None = None,
) -> Any:
    """GET /accounts/{account_id}."""
    return _get(f"/accounts/{account_id}", session=session, settings=settings)

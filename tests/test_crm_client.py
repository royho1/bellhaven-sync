from __future__ import annotations

import logging

import pytest
import requests

from bellhaven_sync import config, crm_client

from .conftest import FAKE_TOKEN, FakeResponse, FakeSession


def account(idx: int) -> dict:
    return {"id": idx, "name": f"Account {idx}"}


def test_get_me_hits_the_me_endpoint(settings):
    session = FakeSession([FakeResponse({"email": "analyst@example.test"})])

    result = crm_client.get_me(session=session, settings=settings)

    assert result == {"email": "analyst@example.test"}
    assert session.calls[0]["url"] == "https://crm.example.test/api/v1/me"


def test_pagination_stops_on_short_page(settings):
    session = FakeSession(
        [
            FakeResponse({"items": [account(1), account(2)], "page": 1}),
            FakeResponse({"items": [account(3)], "page": 2}),
        ]
    )

    accounts = list(crm_client.iter_accounts(page_size=2, session=session, settings=settings))

    assert [a["id"] for a in accounts] == [1, 2, 3]
    assert [call["params"]["page"] for call in session.calls] == [1, 2]


def test_pagination_stops_on_empty_page(settings):
    session = FakeSession(
        [
            FakeResponse({"items": [account(1), account(2)]}),
            FakeResponse({"items": []}),
        ]
    )

    accounts = list(crm_client.iter_accounts(page_size=2, session=session, settings=settings))

    assert len(accounts) == 2
    assert len(session.calls) == 2


def test_pagination_handles_a_bare_list_envelope(settings):
    session = FakeSession([FakeResponse([account(1)])])

    accounts = list(crm_client.iter_accounts(page_size=50, session=session, settings=settings))

    assert accounts == [account(1)]


def test_filters_are_passed_through_and_none_is_dropped(settings):
    session = FakeSession([FakeResponse({"items": []})])

    crm_client.get_accounts(page=1, page_size=25, state="OH", city=None, session=session, settings=settings)

    params = session.calls[0]["params"]
    assert params == {"page": 1, "page_size": 25, "state": "OH"}


def test_unknown_envelope_raises_a_clear_error(settings):
    session = FakeSession([FakeResponse({"unexpected": {"nested": True}})])

    with pytest.raises(crm_client.CrmError) as exc:
        list(crm_client.iter_accounts(session=session, settings=settings))

    assert "Could not find a list of accounts" in str(exc.value)


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "Authentication failed"),
        (403, "Not authorized"),
        (404, "Not found"),
        (422, "Request rejected"),
        (429, "Rate limited"),
    ],
)
def test_error_status_codes_map_to_useful_messages(settings, status, expected):
    session = FakeSession([FakeResponse(status_code=status, text="denied")])

    with pytest.raises(crm_client.CrmError) as exc:
        crm_client.get_me(session=session, settings=settings)

    assert expected in str(exc.value)
    assert exc.value.status_code == status


def test_server_errors_are_retried_then_raised(settings):
    session = FakeSession(
        [
            FakeResponse(status_code=500, text="boom"),
            FakeResponse(status_code=500, text="boom"),
            FakeResponse(status_code=500, text="boom"),
        ]
    )

    with pytest.raises(crm_client.CrmError) as exc:
        crm_client.get_me(session=session, settings=settings)

    assert exc.value.status_code == 500
    assert len(session.calls) == crm_client.MAX_RETRIES


def test_server_error_then_success_recovers(settings):
    session = FakeSession(
        [
            FakeResponse(status_code=503, text="unavailable"),
            FakeResponse({"ok": True}),
        ]
    )

    assert crm_client.get_me(session=session, settings=settings) == {"ok": True}


def test_connection_errors_are_retried_then_raised(settings):
    class ExplodingSession:
        def __init__(self):
            self.attempts = 0

        def get(self, *_args, **_kwargs):
            self.attempts += 1
            raise requests.ConnectionError("network down")

    session = ExplodingSession()

    with pytest.raises(crm_client.CrmError) as exc:
        crm_client.get_me(session=session, settings=settings)

    assert session.attempts == crm_client.MAX_RETRIES
    assert "Could not reach" in str(exc.value)


def test_non_json_response_raises(settings):
    session = FakeSession([FakeResponse(payload=None, text="<html>nope</html>")])

    with pytest.raises(crm_client.CrmError) as exc:
        crm_client.get_me(session=session, settings=settings)

    assert "was not JSON" in str(exc.value)


def test_token_is_sent_as_a_bearer_header(settings):
    session = config.build_session(settings)

    assert session.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"


def test_redact_scrubs_the_token_from_any_string(settings):
    leaked = f"Authorization: Bearer {FAKE_TOKEN} failed"

    scrubbed = config.redact(leaked)

    assert FAKE_TOKEN not in scrubbed
    assert config.REDACTION_PLACEHOLDER in scrubbed


def test_token_never_appears_in_errors_or_logs(settings, caplog):
    session = FakeSession(
        [
            FakeResponse(status_code=401, text=f"invalid token {FAKE_TOKEN}"),
        ]
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(crm_client.CrmError) as exc:
            crm_client.get_me(session=session, settings=settings)

    assert FAKE_TOKEN not in str(exc.value)
    assert FAKE_TOKEN not in caplog.text


def test_connection_error_messages_are_redacted(settings):
    class LeakySession:
        def get(self, *_args, **_kwargs):
            raise requests.ConnectionError(f"failed with token {FAKE_TOKEN}")

    with pytest.raises(crm_client.CrmError) as exc:
        crm_client.get_me(session=LeakySession(), settings=settings)

    assert FAKE_TOKEN not in str(exc.value)


def test_missing_token_raises_a_helpful_config_error(monkeypatch, tmp_path):
    config.reset_settings()
    monkeypatch.delenv("CLIPBOARD_API_TOKEN", raising=False)

    with pytest.raises(config.ConfigError) as exc:
        config.load_settings(env_file=tmp_path / "absent.env", force=True)

    assert "CLIPBOARD_API_TOKEN" in str(exc.value)


def test_module_exposes_no_write_helpers():
    forbidden = {"post", "patch", "put", "delete"}
    exposed = {name.lower() for name in dir(crm_client) if not name.startswith("__")}

    assert not (forbidden & exposed)
    assert not any(
        name.lower().startswith(("post_", "patch_", "put_", "delete_", "create_", "update_"))
        for name in exposed
    )

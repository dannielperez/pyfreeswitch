# ruff: noqa: INP001
"""Typed SIP registration facade tests."""

from __future__ import annotations

import pytest
from pyfreeswitch import ESLClient
from pyfreeswitch import ESLConfig
from pyfreeswitch import NotSupportedError
from pyfreeswitch import SofiaRegState
from pyfreeswitch import parse_sofia_reg
from pyfreeswitch import parse_sofia_reg_result

_AUTH_VALUE = "test-" + "value"

_SOFIA_REG = """
Registrations:
================================================================================
Call-ID: abc-1
User: 101@10.254.250.12
Contact: "101" <sip:101@10.40.1.5:5060;transport=udp>
Status: Registered(UDP)(unknown)
================================================================================
Total items returned: 1
"""


def test_parse_sofia_reg_returns_typed_rows():
    rows = parse_sofia_reg(_SOFIA_REG)

    assert [row.extension for row in rows] == ["101"]
    assert rows[0].contact.startswith('"101"')
    assert rows[0].status_text.startswith("Registered")


def test_client_context_owns_connect_auth_and_close(monkeypatch):
    client = ESLClient(ESLConfig(host="core.example", password=_AUTH_VALUE))
    calls = []
    monkeypatch.setattr(client, "connect", lambda: calls.append("connect"))
    monkeypatch.setattr(client, "authenticate", lambda: calls.append("authenticate"))
    monkeypatch.setattr(client, "close", lambda: calls.append("close"))

    with client as bound:
        assert bound is client

    assert calls == ["connect", "authenticate", "close"]


def test_client_context_closes_when_authentication_fails(monkeypatch):
    client = ESLClient(ESLConfig(host="core.example", password=_AUTH_VALUE))
    calls = []
    error = RuntimeError("auth " + "failed")

    def fail_authentication():
        raise error

    monkeypatch.setattr(client, "connect", lambda: calls.append("connect"))
    monkeypatch.setattr(client, "authenticate", fail_authentication)
    monkeypatch.setattr(client, "close", lambda: calls.append("close"))

    with pytest.raises(RuntimeError), client:
        pass

    assert calls == ["connect", "close"]


def test_list_registrations_owns_command_and_parsing(monkeypatch):
    client = ESLClient(ESLConfig(host="core.example", password=_AUTH_VALUE))
    commands = []

    def fake_api(command, **kwargs):
        commands.append(command)
        return _SOFIA_REG

    monkeypatch.setattr(client, "api", fake_api)

    rows = client.list_registrations("wg")

    assert commands == ["sofia status profile wg reg"]
    assert [row.extension for row in rows] == ["101"]


def test_list_registrations_rejects_command_injection():
    client = ESLClient(ESLConfig(host="core.example", password=_AUTH_VALUE))

    with pytest.raises(NotSupportedError):
        client.list_registrations("wg; status")


# -- parse_sofia_reg_result: why an empty list is empty -----------------------
#
# parse_sofia_reg returns a bare list, so a profile with no registrations, an
# -ERR refusal, and truncated output are indistinguishable. A consumer that
# reads all three as "nobody is registered" will unregister a whole profile's
# endpoints on a transport hiccup. These pin the state each reply maps to.

_SOFIA_REG_EMPTY = """
Registrations:
================================================================================
Total items returned: 0
"""


def test_result_reports_complete_when_rows_parse():
    result = parse_sofia_reg_result(_SOFIA_REG)

    assert result.state is SofiaRegState.COMPLETE
    assert [row.extension for row in result.registrations] == ["101"]
    assert result.is_authoritative is True


def test_result_distinguishes_authoritative_empty_from_malformed():
    """The distinction the bare-list API cannot express."""
    empty = parse_sofia_reg_result(_SOFIA_REG_EMPTY)
    malformed = parse_sofia_reg_result("<html>502 Bad Gateway</html>")

    assert empty.state is SofiaRegState.EMPTY
    assert empty.is_authoritative is True

    assert malformed.state is SofiaRegState.MALFORMED
    assert malformed.is_authoritative is False

    # Both yield zero rows — which is exactly why state has to carry the truth.
    assert empty.registrations == malformed.registrations == []


@pytest.mark.parametrize(
    "reply",
    [
        "-ERR Invalid Profile",
        "-err no such profile",
        "-USAGE: sofia status profile <name>",
    ],
)
def test_result_reports_error_for_freeswitch_refusals(reply):
    result = parse_sofia_reg_result(reply)

    assert result.state is SofiaRegState.ERROR
    assert result.is_authoritative is False
    assert result.detail


def test_error_wins_over_incidentally_parseable_rows():
    """An -ERR reply is an error even if a User: line survives in it."""
    result = parse_sofia_reg_result("-ERR truncated\nUser: 101@core\n")

    assert result.state is SofiaRegState.ERROR
    assert result.registrations == []


def test_no_bytes_at_all_is_malformed_not_empty():
    """A dropped read looks identical to an empty profile — assume neither."""
    for blank in ("", "   \n\t "):
        result = parse_sofia_reg_result(blank)
        assert result.state is SofiaRegState.MALFORMED
        assert result.is_authoritative is False


def test_error_detail_keeps_only_the_first_line():
    result = parse_sofia_reg_result("-ERR bad profile\nsofia status profile SECRET reg")

    assert result.detail == "-ERR bad profile"


def test_parse_sofia_reg_is_unchanged_for_existing_callers():
    """The bare-list API keeps its contract — this is additive."""
    assert [row.extension for row in parse_sofia_reg(_SOFIA_REG)] == ["101"]
    assert parse_sofia_reg(_SOFIA_REG_EMPTY) == []
    assert parse_sofia_reg("") == []

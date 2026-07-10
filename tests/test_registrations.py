# ruff: noqa: INP001
"""Typed SIP registration facade tests."""

from __future__ import annotations

import pytest
from pyfreeswitch import ESLClient
from pyfreeswitch import ESLConfig
from pyfreeswitch import NotSupportedError
from pyfreeswitch import parse_sofia_reg

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

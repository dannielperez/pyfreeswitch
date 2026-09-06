# ruff: noqa: INP001, SLF001
"""uuid_* typed call-control writers: exact command forms, gates, replies."""

from __future__ import annotations

import pytest
from pyfreeswitch import ESLClient
from pyfreeswitch import ESLConfig
from pyfreeswitch import NotSupportedError
from pyfreeswitch import parse_command_reply

_AUTH_VALUE = "test-" + "value"
_UUID = "6af16dae-d71a-4e3f-ac67-642bc60a5300"


def _client(*, allow_mutations: bool = True) -> tuple[ESLClient, list[str]]:
    client = ESLClient(
        ESLConfig(
            host="core.example",
            password=_AUTH_VALUE,
            allow_mutations=allow_mutations,
        ),
    )
    sent: list[str] = []
    client._command = lambda line: sent.append(line) or {"_body": "+OK\n"}
    return client, sent


# -- parse_command_reply -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("+OK\n", True),
        ("+OK Success\n", True),
        ("+OK Message sent\n", True),
        ("-ERR No such channel!\n", False),
        ("-ERR\n", False),
        ("-USAGE: <uuid> [cause]\n", False),
        ("", False),
        ("garbage", False),
    ],
)
def test_parse_command_reply_classifies_status_line(text, ok):
    assert parse_command_reply(text).ok is ok


def test_parse_command_reply_not_found_flag():
    assert parse_command_reply("-ERR No such channel!\n").not_found is True
    assert parse_command_reply("-ERR Cannot locate session!\n").not_found is True
    assert parse_command_reply("-ERR Agent not found!\n").not_found is True
    assert parse_command_reply("-ERR Cannot record session!\n").not_found is False
    assert parse_command_reply("+OK\n").not_found is False


def test_parse_command_reply_keeps_first_line_only():
    reply = parse_command_reply("-ERR bad\nuuid_kill SECRET-ish\n")

    assert reply.detail == "-ERR bad"


# -- uuid_kill -----------------------------------------------------------------


def test_uuid_kill_exact_form():
    client, sent = _client()

    assert client.uuid_kill(_UUID).ok is True
    assert sent == [f"api uuid_kill {_UUID}"]


def test_uuid_kill_with_cause():
    client, sent = _client()

    client.uuid_kill(_UUID, cause="NORMAL_CLEARING")

    assert sent == [f"api uuid_kill {_UUID} NORMAL_CLEARING"]


@pytest.mark.parametrize(
    "bad_uuid",
    ["", "not-a-uuid", f"{_UUID} extra", f"{_UUID}\nhupall", "6af16dae"],
)
def test_uuid_kill_rejects_bad_uuid(bad_uuid):
    client, sent = _client()

    with pytest.raises(NotSupportedError):
        client.uuid_kill(bad_uuid)

    assert sent == []


def test_uuid_kill_rejects_bad_cause():
    client, sent = _client()

    with pytest.raises(NotSupportedError):
        client.uuid_kill(_UUID, cause="normal clearing; hupall")

    assert sent == []


def test_uuid_kill_gated_by_allow_mutations():
    client, sent = _client(allow_mutations=False)

    with pytest.raises(NotSupportedError, match="read-only"):
        client.uuid_kill(_UUID)

    assert sent == []


def test_uuid_kill_reports_no_such_channel():
    client, _ = _client()
    client._command = lambda _line: {"_body": "-ERR No such channel!\n"}

    reply = client.uuid_kill(_UUID)

    assert reply.ok is False
    assert reply.not_found is True


# -- uuid_transfer -------------------------------------------------------------


def test_uuid_transfer_default_aleg_form():
    client, sent = _client()

    client.uuid_transfer(_UUID, "1901")

    assert sent == [f"api uuid_transfer {_UUID} 1901 XML"]


def test_uuid_transfer_bleg_with_context():
    client, sent = _client()

    client.uuid_transfer(_UUID, "1901", leg="bleg", context="default")

    assert sent == [f"api uuid_transfer {_UUID} -bleg 1901 XML default"]


def test_uuid_transfer_both_legs():
    client, sent = _client()

    client.uuid_transfer(_UUID, "*98", leg="both", dialplan="XML", context="public")

    assert sent == [f"api uuid_transfer {_UUID} -both *98 XML public"]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"destination": "1901 extra"}, "destination"),
        ({"destination": "-bleg"}, "destination"),
        ({"destination": "1901", "leg": "cleg"}, "leg"),
        ({"destination": "1901", "dialplan": "XML foo"}, "dialplan"),
        ({"destination": "1901", "context": "def ault"}, "context"),
    ],
)
def test_uuid_transfer_rejects_bad_tokens(kwargs, match):
    client, sent = _client()

    with pytest.raises(NotSupportedError, match=match):
        client.uuid_transfer(_UUID, **kwargs)

    assert sent == []


# -- attended transfer (att_xfer via uuid_broadcast) ---------------------------


def test_uuid_attended_transfer_exact_form():
    client, sent = _client()

    reply = client.uuid_attended_transfer(_UUID, "user/1901")

    assert reply.ok is True
    assert sent == [f"api uuid_broadcast {_UUID} att_xfer::user/1901 aleg"]


def test_uuid_attended_transfer_accepts_sofia_dialstring():
    client, sent = _client()

    client.uuid_attended_transfer(_UUID, "sofia/internal/1901@core.example")

    assert sent == [
        f"api uuid_broadcast {_UUID} att_xfer::sofia/internal/1901@core.example aleg",
    ]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "user/1901 bleg",
        "{origination_caller_id_number=1}user/1901",
        "user/1901\nhupall",
    ],
)
def test_uuid_attended_transfer_rejects_variables_and_whitespace(bad):
    client, sent = _client()

    with pytest.raises(NotSupportedError):
        client.uuid_attended_transfer(_UUID, bad)

    assert sent == []


# -- uuid_record ---------------------------------------------------------------


def test_uuid_record_start_and_stop_forms():
    client, sent = _client()

    client.uuid_record(_UUID, "start", "/var/lib/freeswitch/recordings/x.wav")
    client.uuid_record(_UUID, "stop", "/var/lib/freeswitch/recordings/x.wav")

    assert sent == [
        f"api uuid_record {_UUID} start /var/lib/freeswitch/recordings/x.wav",
        f"api uuid_record {_UUID} stop /var/lib/freeswitch/recordings/x.wav",
    ]


@pytest.mark.parametrize(
    ("action", "path"),
    [
        ("mask", "/var/lib/freeswitch/recordings/x.wav"),
        ("start", "relative.wav"),
        ("start", "/var/lib/freeswitch/../etc/x.wav"),
        ("start", "/var/lib/freeswitch/recordings/x.wav 10"),
        ("start", "/var/lib/freeswitch/recordings/x.wav {a=b}"),
    ],
)
def test_uuid_record_rejects_unsupported_actions_and_paths(action, path):
    client, sent = _client()

    with pytest.raises(NotSupportedError):
        client.uuid_record(_UUID, action, path)

    assert sent == []


def test_uuid_record_reports_cannot_locate_session():
    client, _ = _client()
    client._command = lambda _line: {"_body": "-ERR Cannot locate session!\n"}

    reply = client.uuid_record(_UUID, "start", "/var/lib/freeswitch/recordings/x.wav")

    assert reply.ok is False
    assert reply.not_found is True


# -- raw api() gate is unchanged ----------------------------------------------


def test_raw_api_still_refuses_mutations_even_with_allow_mutations():
    """allow_mutations unlocks the *typed* writers only, never raw strings."""
    client, sent = _client(allow_mutations=True)

    with pytest.raises(NotSupportedError):
        client.api(f"uuid_kill {_UUID}")

    assert sent == []


def test_read_only_allow_list_accepts_new_typed_read_forms():
    client, sent = _client(allow_mutations=False)

    client.api("callcenter_config agent list 1900@default")
    client.api("callcenter_config agent get status 1900@default")
    client.api("callcenter_config agent get state 1900@default")

    assert len(sent) == 3


def test_read_only_allow_list_rejects_get_with_unknown_key():
    client, sent = _client(allow_mutations=False)

    with pytest.raises(NotSupportedError):
        client.api("callcenter_config agent get contact 1900@default")

    assert sent == []

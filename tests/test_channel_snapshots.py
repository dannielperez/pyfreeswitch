"""Completeness-aware live channel snapshots, without a FreeSWITCH server."""

import json
import socket
from datetime import datetime, timezone

import pytest

from esl_peer import ScriptedPeer, accepted, frame
from pyfreeswitch import ESLClient, ESLConfig
from pyfreeswitch.models.channels import ChannelSnapshotState, parse_channel_snapshot

UUID = "13f26b5d-a837-4638-beb0-3fe74b01d3fa"
ROW = {
    "uuid": UUID,
    "direction": "inbound",
    "state": "CS_EXECUTE",
    "cid_name": "José",
    "cid_num": "1001",
    "dest": "99",
    "created_epoch": "1788998400",
    "call_uuid": UUID,
}


def response(rows):
    return json.dumps({"row_count": len(rows), "rows": rows})


def test_complete_snapshot_maps_typed_fields_and_ignores_extensions():
    result = parse_channel_snapshot(
        response([{**ROW, "future_field": {"ignored": True}}])
    )
    assert result.state is ChannelSnapshotState.COMPLETE
    assert result.is_authoritative
    assert result.row_count == 1
    assert result.channels[0].uuid == UUID
    assert result.channels[0].caller_name == "José"
    assert result.channels[0].created_epoch == 1788998400


@pytest.mark.parametrize("body", ['{"row_count":0}', '{"row_count":0,"rows":[]}'])
def test_explicit_empty_server_shapes(body):
    result = parse_channel_snapshot(body)
    assert result.state is ChannelSnapshotState.EMPTY
    assert result.is_authoritative
    assert result.channels == ()


@pytest.mark.parametrize(
    "body",
    [
        "",
        "{}",
        "null",
        "[]",
        '{"row_count":1}',
        '{"row_count":true,"rows":[]}',
        '{"row_count":"0"}',
        '{"row_count":-1}',
        '{"row_count":0,"rows":null}',
        '{"row_count":0,"row_count":1}',
        '{"row_count":NaN}',
        '{"row_count":0} trailing',
        '{"row_count":0,"rows":[{}]}',
    ],
)
def test_malformed_is_never_authoritative_empty(body):
    result = parse_channel_snapshot(body)
    assert result.state is ChannelSnapshotState.MALFORMED
    assert not result.is_authoritative
    assert result.channels == ()


@pytest.mark.parametrize(
    "change",
    [
        {"uuid": "bad"},
        {"uuid": None},
        {"direction": "unknown"},
        {"state": ""},
        {"cid_num": 123},
        {"created_epoch": "-1"},
        {"created_epoch": True},
        {"call_uuid": "bad"},
    ],
)
def test_invalid_row_invalidates_whole_snapshot(change):
    second = {**ROW, "uuid": "13f26b5d-a837-4638-beb0-3fe74b01d3fb", **change}
    result = parse_channel_snapshot(response([ROW, second]))
    assert not result.is_authoritative
    assert result.channels == ()


def test_duplicate_channel_identity_is_not_complete():
    assert not parse_channel_snapshot(response([ROW, ROW])).is_authoritative


def test_optional_columns_can_be_absent():
    result = parse_channel_snapshot(
        response(
            [
                {"uuid": UUID, "direction": "outbound", "state": "CS_NEW"},
            ]
        )
    )
    assert result.is_authoritative
    assert result.channels[0].created_epoch is None
    assert result.channels[0].caller_name == ""


def test_count_mismatch_is_not_complete():
    result = parse_channel_snapshot(json.dumps({"row_count": 2, "rows": [ROW]}))
    assert not result.is_authoritative


def test_row_budget_is_enforced():
    assert not parse_channel_snapshot('{"row_count":10001}').is_authoritative


def test_deep_json_does_not_escape_as_recursion_error():
    assert not parse_channel_snapshot("[" * 2000 + "]" * 2000).is_authoritative


def test_payload_byte_budget_is_enforced(monkeypatch):
    monkeypatch.setattr("pyfreeswitch.models.channels.MAX_ESL_MAX_BODY_BYTES", 32)
    assert not parse_channel_snapshot(" " * 33).is_authoritative
    assert not parse_channel_snapshot("é" * 20).is_authoritative


def test_snapshot_top_level_exports():
    from pyfreeswitch import ChannelSnapshot, LiveChannel

    assert isinstance(parse_channel_snapshot(response([ROW])), ChannelSnapshot)
    assert isinstance(parse_channel_snapshot(response([ROW])).channels[0], LiveChannel)


@pytest.mark.parametrize(
    "body", ["-ERR SQL error sensitive", '-USAGE: secret\n{"row_count":0}']
)
def test_refusal_does_not_leak_server_text(body):
    result = parse_channel_snapshot(body)
    assert result.state is ChannelSnapshotState.ERROR
    assert not result.is_authoritative
    assert "secret" not in result.detail and "sensitive" not in result.detail


@pytest.mark.parametrize("fragment_size", [1, 7, 4096])
def test_snapshot_through_real_client_with_racing_event(monkeypatch, fragment_size):
    event = frame("text/event-plain", "Event-Name: HEARTBEAT\n\n")
    peer = ScriptedPeer(
        [
            (b"auth synthetic\n\n", [accepted()]),
            (
                b"api show channels as json\n\n",
                [event + frame("api/response", response([ROW]))],
            ),
        ],
        fragment_size=fragment_size,
    )
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_kw: peer)
    before = datetime.now(timezone.utc)
    with ESLClient(ESLConfig(host="test.invalid", password="synthetic")) as client:
        result = client.list_channels_result()
        assert result.is_authoritative
        assert result.source_host == "test.invalid"
        assert result.source_port == 8021
        assert before <= result.captured_at <= datetime.now(timezone.utc)
        assert client.read_event()["Event-Name"] == "HEARTBEAT"
    peer.assert_complete()
    assert peer.closed

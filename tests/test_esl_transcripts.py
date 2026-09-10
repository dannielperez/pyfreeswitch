"""Full connection transcripts through a deterministic peer, never vendor I/O."""

import socket
from collections import deque

import pytest

from esl_peer import ScriptedPeer, accepted, frame
from pyfreeswitch import ESLClient, ESLConfig, ESLConnectionError, ESLTimeout


@pytest.mark.parametrize("fragment_size", [1, 2, 3, 7, 4096])
def test_subscription_unicode_payload_and_command_order(monkeypatch, fragment_size):
    payload = "José\nnext: literal%20text"
    event = frame(
        "text/event-plain",
        (
            "Event-Name: CUSTOM\nEvent-Subclass: example::body\n"
            "Caller-Caller-ID-Name: Jos%C3%A9\n"
            f"Content-Length: {len(payload.encode())}\n\n{payload}"
        ),
    )
    peer = ScriptedPeer(
        [
            (b"auth synthetic\n\n", [accepted()]),
            (b"event plain CUSTOM example::body\n\n", [event + accepted()]),
            (b"api status\n\n", [frame("api/response", "UP é")]),
        ],
        fragment_size=fragment_size,
    )
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_kw: peer)
    with ESLClient(ESLConfig(host="test.invalid", password="synthetic")) as client:
        client.subscribe(["example::body"])
        assert client.status() == "UP é"
        result = client.read_event()
        assert result["Caller-Caller-ID-Name"] == "José"
        assert result["_body"] == payload
    peer.assert_complete()


def test_typed_mutation_timeout_is_not_replayed(monkeypatch):
    uuid = "13f26b5d-a837-4638-beb0-3fe74b01d3fa"
    peer = ScriptedPeer(
        [
            (b"auth synthetic\n\n", [accepted()]),
            (
                f"api uuid_kill {uuid}\n\n".encode(),
                [None, frame("api/response", "+OK")],
            ),
        ]
    )
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_kw: peer)
    with ESLClient(
        ESLConfig(host="test.invalid", password="synthetic", allow_mutations=True)
    ) as client:
        with pytest.raises(ESLTimeout):
            client.uuid_kill(uuid)
        assert not client.connected
        with pytest.raises(ESLConnectionError):
            client.status()
    assert len(peer.sent) == 2
    peer.assert_complete()


@pytest.mark.parametrize("reply", [b"Content-T", b"Content-Length: 20\n\nshort"])
def test_partial_command_frame_eof_closes_connection(monkeypatch, reply):
    peer = ScriptedPeer(
        [
            (b"auth synthetic\n\n", [accepted()]),
            (b"api status\n\n", [reply, b""]),
        ]
    )
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_kw: peer)
    with ESLClient(ESLConfig(host="test.invalid", password="synthetic")) as client:
        with pytest.raises(ESLConnectionError):
            client.status()
        assert not client.connected
    peer.assert_complete()


def test_greeting_timeout_releases_socket(monkeypatch):
    peer = ScriptedPeer([])
    peer.pending = deque([None])
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_kw: peer)
    client = ESLClient(ESLConfig(host="test.invalid", password="synthetic"))
    with pytest.raises(ESLTimeout):
        with client:
            pytest.fail("greeting timeout must prevent context entry")
    assert peer.closed
    assert not client.connected


def test_socket_setup_failure_releases_socket(monkeypatch):
    peer = ScriptedPeer([])

    def broken_option(*_args):
        raise OSError("synthetic socket option failure")

    peer.setsockopt = broken_option
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_kw: peer)
    client = ESLClient(ESLConfig(host="test.invalid", password="synthetic"))
    with pytest.raises(OSError):
        client.connect()
    assert peer.closed
    assert not client.connected

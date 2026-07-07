"""ESL socket framing/auth against a fake in-memory socket."""

from __future__ import annotations

import socket

import pytest

from pyfreeswitch.clients.esl import ESLClient
from pyfreeswitch.config import ESLConfig
from pyfreeswitch.exceptions import ESLAuthError, ESLTimeout, NotSupportedError


class FakeSocket:
    """Feeds queued byte chunks to recv(); records sent bytes.

    A chunk of ``None`` raises socket.timeout on that recv() (idle window);
    an empty ``b""`` chunk models a peer close.
    """

    def __init__(self, chunks: list[bytes | None]) -> None:
        self._chunks = list(chunks)
        self.sent: list[bytes] = []
        self.closed = False

    def recv(self, _bufsize: int) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if chunk is None:
            raise socket.timeout
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def settimeout(self, _t: float) -> None:  # noqa: D401 - stub
        pass

    def close(self) -> None:
        self.closed = True


def _client(chunks: list[bytes | None]) -> ESLClient:
    cfg = ESLConfig(host="h", port=8021, password="secret")
    client = ESLClient(cfg)
    client._sock = FakeSocket(chunks)  # inject transport
    client._connected = True
    return client


def test_authenticate_accepts_ok_reply() -> None:
    client = _client([b"Content-Type: command/reply\nReply-Text: +OK accepted\n\n"])
    client.authenticate()
    assert client.authenticated is True
    # Password is sent, never stored in the reply path.
    assert b"auth secret\n\n" in b"".join(client._sock.sent)


def test_authenticate_rejects_bad_password() -> None:
    client = _client([b"Content-Type: command/reply\nReply-Text: -ERR invalid\n\n"])
    with pytest.raises(ESLAuthError):
        client.authenticate()


def test_read_event_unwraps_event_plain_and_url_decodes() -> None:
    body = (
        "Event-Name: CHANNEL_CREATE\n"
        "Unique-ID: leg-1\n"
        "Channel-Call-UUID: call-1\n"
        "Caller-Destination-Number: 99\n"
        "Caller-Caller-ID-Name: Los%20Olmos\n"
        "\n"
    )
    frame = (
        f"Content-Length: {len(body)}\n"
        "Content-Type: text/event-plain\n"
        "\n"
        f"{body}"
    ).encode()
    client = _client([frame])
    event = client.read_event()
    assert event["Event-Name"] == "CHANNEL_CREATE"
    assert event["Caller-Destination-Number"] == "99"
    assert event["Caller-Caller-ID-Name"] == "Los Olmos"  # %20 decoded


def test_read_event_split_across_recv_chunks() -> None:
    body = "Event-Name: CHANNEL_ANSWER\nUnique-ID: leg-2\n\n"
    frame = (
        f"Content-Length: {len(body)}\nContent-Type: text/event-plain\n\n{body}"
    ).encode()
    mid = len(frame) // 2
    client = _client([frame[:mid], frame[mid:]])
    event = client.read_event()
    assert event["Event-Name"] == "CHANNEL_ANSWER"


def test_recv_timeout_raises_esl_timeout() -> None:
    client = _client([None])
    with pytest.raises(ESLTimeout):
        client.read_event()


def test_api_blocks_unsafe_verb() -> None:
    client = _client([])
    with pytest.raises(NotSupportedError):
        client.api("originate sofia/x")


def test_api_allows_status_verb() -> None:
    reply = (
        "Content-Type: api/response\nContent-Length: 3\n\nUP\n"
    ).encode()
    client = _client([reply])
    out = client.api("status")
    assert "UP" in out

"""ESL socket framing/auth against a fake in-memory socket."""
# ruff: noqa: INP001, S106, SLF001

from __future__ import annotations

import socket

import pytest
from pyfreeswitch.clients.esl import ESLClient
from pyfreeswitch.clients.esl_listener import ESLEventListener
from pyfreeswitch.config import MIN_ESL_MAX_BODY_BYTES
from pyfreeswitch.config import MIN_ESL_MAX_FRAME_BYTES
from pyfreeswitch.config import MIN_ESL_MAX_HEADER_BYTES
from pyfreeswitch.config import ESLConfig
from pyfreeswitch.exceptions import ESLAuthError
from pyfreeswitch.exceptions import ESLConnectionError
from pyfreeswitch.exceptions import ESLError
from pyfreeswitch.exceptions import ESLProtocolError
from pyfreeswitch.exceptions import ESLTimeout
from pyfreeswitch.exceptions import NotSupportedError


class FakeSocket:
    """Feeds queued byte chunks to recv(); records sent bytes.

    A chunk of ``None`` raises socket.timeout on that recv() (idle window);
    an empty ``b""`` chunk models a peer close.
    """

    def __init__(self, chunks: list[bytes | None]) -> None:
        self._chunks = list(chunks)
        self.sent: list[bytes] = []
        self.timeouts: list[float] = []
        self.recv_calls = 0
        self.closed = False

    def recv(self, _bufsize: int) -> bytes:
        self.recv_calls += 1
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if chunk is None:
            raise socket.timeout  # noqa: UP041 - models the socket API exactly
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def close(self) -> None:
        self.closed = True


def _client(chunks: list[bytes | None], **config_overrides: object) -> ESLClient:
    cfg = ESLConfig(
        host="h",
        port=8021,
        password="secret",
        **config_overrides,
    )
    client = ESLClient(cfg)
    client._sock = FakeSocket(chunks)  # inject transport
    client._connected = True
    return client


def _api_reply(body: str = "") -> bytes:
    return (
        f"Content-Type: api/response\nContent-Length: {len(body)}\n\n{body}"
    ).encode()


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
        f"Content-Length: {len(body)}\nContent-Type: text/event-plain\n\n{body}"
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
    assert client.connected is True
    assert client._sock.closed is False


def test_oversize_header_fails_at_cap_and_discards_buffer() -> None:
    client = _client(
        [b"X" * 600, b"Y" * 600],
        max_header_bytes=MIN_ESL_MAX_HEADER_BYTES,
    )

    with pytest.raises(ESLProtocolError, match="header block exceeds"):
        client.read_event()

    assert client._buffer == b""
    assert client._sock is None


def test_oversize_body_is_rejected_without_body_read() -> None:
    announced = MIN_ESL_MAX_BODY_BYTES + 1
    client = _client(
        [f"Content-Length: {announced}\n\n".encode()],
        max_body_bytes=MIN_ESL_MAX_BODY_BYTES,
    )
    sock = client._sock

    with pytest.raises(ESLProtocolError, match="maximum body size"):
        client.read_event()

    assert sock.recv_calls == 1


def test_oversize_total_frame_is_rejected_without_body_read() -> None:
    announced = MIN_ESL_MAX_FRAME_BYTES
    client = _client(
        [f"Content-Length: {announced}\n\n".encode()],
        max_body_bytes=announced,
        max_frame_bytes=MIN_ESL_MAX_FRAME_BYTES,
    )
    sock = client._sock

    with pytest.raises(ESLProtocolError, match="maximum total frame size"):
        client.read_event()

    assert sock.recv_calls == 1


@pytest.mark.parametrize("length", ["not-a-number", "-1"])
def test_malformed_outer_content_length_fails(length: str) -> None:
    client = _client([f"Content-Length: {length}\n\n".encode()])

    with pytest.raises(ESLProtocolError, match="invalid Content-Length"):
        client.read_event()


def test_protocol_error_is_connection_error() -> None:
    assert issubclass(ESLProtocolError, ESLConnectionError)
    assert issubclass(ESLProtocolError, ESLError)


def test_listener_reconnects_after_protocol_error(monkeypatch) -> None:
    config = ESLConfig(host="h", port=8021, password="secret")
    first_socket = FakeSocket(
        [
            b"Content-Type: auth/request\n\n",
            b"Content-Type: command/reply\nReply-Text: +OK accepted\n\n",
            b"Content-Type: command/reply\nReply-Text: +OK subscribed\n\n",
            b"Content-Length: not-a-number\n\n",
        ],
    )
    second_socket = FakeSocket(
        [
            b"Content-Type: auth/request\n\n",
            b"Content-Type: command/reply\nReply-Text: +OK accepted\n\n",
            b"Content-Type: command/reply\nReply-Text: +OK subscribed\n\n",
        ],
    )
    sockets = iter([first_socket, second_socket])
    connection_attempts = 0
    expected_connection_attempts = 2
    sleeps: list[float] = []
    listener = ESLEventListener(config, sleep=sleeps.append)

    def create_connection(
        _address: tuple[str, int],
        *,
        timeout: float,
    ) -> FakeSocket:
        nonlocal connection_attempts
        connection_attempts += 1
        if connection_attempts == expected_connection_attempts:
            listener.close()
        sock = next(sockets)
        sock.settimeout(timeout)
        return sock

    monkeypatch.setattr(socket, "create_connection", create_connection)

    listener.run_forever(lambda _event: pytest.fail("unexpected event"))

    assert connection_attempts == expected_connection_attempts
    assert sleeps == [1.0]
    assert first_socket.closed is True
    assert second_socket.closed is True


@pytest.mark.parametrize(
    "length",
    ["not-a-number", "-1", str(MIN_ESL_MAX_BODY_BYTES + 1)],
)
def test_invalid_inner_event_content_length_fails(length: str) -> None:
    client = _client([], max_body_bytes=MIN_ESL_MAX_BODY_BYTES)
    body = f"Event-Name: CUSTOM\nContent-Length: {length}\n\n"

    with pytest.raises(ESLProtocolError):
        client._parse_event_body(body)


def test_trickle_cannot_extend_total_frame_deadline(monkeypatch) -> None:
    clock = iter([0.0, 0.4, 0.8, 1.0])
    monkeypatch.setattr(
        "pyfreeswitch.clients.esl.time.monotonic",
        lambda: next(clock, 1.0),
    )
    client = _client([b"X"] * 10, timeout=1)
    sock = client._sock

    with pytest.raises(ESLTimeout, match="frame deadline elapsed"):
        client.read_event()

    assert sock.recv_calls == 2  # noqa: PLR2004 - exact controlled-clock contract
    assert client.connected is True


def test_api_blocks_unsafe_verb() -> None:
    client = _client([])
    with pytest.raises(NotSupportedError):
        client.api("originate sofia/x")


@pytest.mark.parametrize(
    "command",
    [
        "sofia profile internal stop",
        "sofia profile internal restart",
        "sofia recover",
        "sofia global siptrace on",
        "sofia loglevel all 9",
        "callcenter_config agent add x y",
        "callcenter_config queue load x",
        "callcenter_config agent set status x y",
        "show arbitrary",
        "show channels as json",
    ],
)
def test_api_rejects_mutating_subcommands(command: str) -> None:
    client = _client([])

    with pytest.raises(NotSupportedError, match=command):
        client.api(command)


@pytest.mark.parametrize(
    "command",
    [
        "status",
        "version",
        "uptime",
        "help",
        "show channels",
        "sofia status",
        "sofia xmlstatus",
        "sofia status profile wg",
        "sofia status profile wg reg",
        "callcenter_config queue list",
    ],
)
def test_api_allows_exact_read_only_forms(command: str) -> None:
    client = _client([_api_reply("OK")])

    assert client.api(command) == "OK"


def test_allow_unsafe_bypasses_read_only_gate() -> None:
    client = _client([_api_reply("OK")])

    assert client.api("sofia profile internal stop", allow_unsafe=True) == "OK"


@pytest.mark.parametrize(
    "command",
    [
        "sofia status profile ../../etc reg",
        "sofia status profile wg;status reg",
        "sofia status profile wg status reg",
    ],
)
def test_api_rejects_invalid_profile_argument(command: str) -> None:
    client = _client([])

    with pytest.raises(NotSupportedError, match="not an allowed read-only form"):
        client.api(command)


def test_typed_read_only_wrappers_route_through_api_gate() -> None:
    client = _client([_api_reply()] * 7)

    assert client.status() == ""
    assert client.sofia_status() == ""
    assert client.sofia_status("wg") == ""
    assert client.sofia_xmlstatus() == ""
    assert client.list_callcenter_queues() == ""
    assert client.list_callcenter_agents() == ""
    assert client.list_callcenter_tiers() == ""
    assert b"".join(client._sock.sent) == (
        b"api status\n\n"
        b"api sofia status\n\n"
        b"api sofia status profile wg\n\n"
        b"api sofia xmlstatus\n\n"
        b"api callcenter_config queue list\n\n"
        b"api callcenter_config agent list\n\n"
        b"api callcenter_config tier list\n\n"
    )

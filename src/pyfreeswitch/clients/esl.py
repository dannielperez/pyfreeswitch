"""FreeSWITCH Event Socket Layer (ESL) inbound client.

ESL is a line-oriented TCP control protocol. Each message is a block of
``Key: Value`` header lines terminated by a blank line; a ``Content-Length``
header means a body of exactly that many bytes follows. Event frames arrive as
``Content-Type: text/event-plain`` whose body is itself a header block (the
event), with header values **URL-encoded**.

Reference: https://developer.signalwire.com/freeswitch/FreeSWITCH-Explained/Modules/mod_event_socket_1048924/

Design Notes
~~~~~~~~~~~~
ESL exposes powerful control (``originate``, ``uuid_kill``, ``reloadxml``,
``fsctl``, ``hupall`` …). This client is **read-only by default**: only
exact, validated query forms run through :meth:`api`; broad verbs with mutating
subcommands are rejected. Anything else raises :class:`NotSupportedError`
unless the caller passes ``allow_unsafe=True`` — an explicit, auditable opt-in.
The listener path (auth + ``event plain`` subscribe + stream) issues no commands
at all beyond the subscription.
"""

from __future__ import annotations

import socket
import time
from contextlib import suppress
from typing import NoReturn
from urllib.parse import unquote

from pyfreeswitch.config import ESLConfig
from pyfreeswitch.config import normalize_sip_profiles
from pyfreeswitch.exceptions import ESLAuthError
from pyfreeswitch.exceptions import ESLConnectionError
from pyfreeswitch.exceptions import ESLError
from pyfreeswitch.exceptions import ESLProtocolError
from pyfreeswitch.exceptions import ESLTimeout
from pyfreeswitch.exceptions import NotSupportedError
from pyfreeswitch.logging import get_logger
from pyfreeswitch.models.registrations import SIPRegistration
from pyfreeswitch.models.registrations import SofiaRegResult
from pyfreeswitch.models.registrations import parse_sofia_reg
from pyfreeswitch.models.registrations import parse_sofia_reg_result

log = get_logger("clients.esl")

_LF = "\n"
_END = "\n\n"

# Complete read-only command shapes. Multi-purpose verbs such as ``sofia`` and
# ``callcenter_config`` are accepted only after their full token sequence is
# matched below.
_SAFE_BARE_API_COMMANDS: frozenset[str] = frozenset(
    {"status", "version", "uptime", "help"},
)
_SAFE_SHOW_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "bridged_calls",
        "calls",
        "channels",
        "detailed_bridged_calls",
        "detailed_calls",
        "registrations",
    },
)
_SAFE_CALLCENTER_LIST_TARGETS: frozenset[str] = frozenset({"agent", "queue", "tier"})


def _is_valid_sip_profile(profile: str) -> bool:
    return normalize_sip_profiles([profile]) == (profile,)


def _is_read_only_api_command(command: str) -> bool:
    """Return whether the complete command matches a sanctioned query form."""
    parts = command.strip().split()
    allowed = False
    match parts:
        case [bare]:
            allowed = bare in _SAFE_BARE_API_COMMANDS
        case ["show", subcommand]:
            allowed = subcommand in _SAFE_SHOW_SUBCOMMANDS
        case ["sofia", query]:
            allowed = query in {"status", "xmlstatus"}
        case ["callcenter_config", target, "list"]:
            allowed = target in _SAFE_CALLCENTER_LIST_TARGETS
        case ["sofia", "status", "profile", profile]:
            allowed = _is_valid_sip_profile(profile)
        case ["sofia", "status", "profile", profile, "reg"]:
            allowed = _is_valid_sip_profile(profile)
    return allowed


class ESLClient:
    """Low-level inbound event-socket transport for one FreeSWITCH core.

    Handles the TCP lifecycle, authentication, event subscription, and framed
    reads. Typed event parsing lives in
    :mod:`pyfreeswitch.clients.esl_parser`; this client only produces raw
    ``{header: value}`` frames.

    Usage::

        from pyfreeswitch.config import ESLConfig
        from pyfreeswitch.clients.esl import ESLClient

        cfg = ESLConfig(host="10.254.250.12", port=8021, password="s3cret")
        client = ESLClient(cfg)
        client.connect()
        client.authenticate()
        client.subscribe(["CHANNEL_CREATE", "CHANNEL_HANGUP_COMPLETE"])
        frame = client.read_event()   # -> dict or raises ESLTimeout / drop
    """

    def __init__(self, config: ESLConfig) -> None:
        self._config = config
        self._sock: socket.socket | None = None
        self._buffer: bytes = b""
        self._connected = False
        self._authenticated = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    def connect(self) -> None:
        """Open the TCP connection and consume the ``auth/request`` greeting.

        Raises:
            ESLConnectionError: If the TCP connection fails or the greeting is
                not an auth request.
        """
        host, port = self._config.host, self._config.port
        log.debug("Connecting to ESL at %s:%d", host, port)
        try:
            sock = socket.create_connection((host, port), timeout=self._config.timeout)
        except OSError as exc:
            log.exception("ESL connection failed")
            msg = f"Failed to connect to ESL at {host}:{port}: {exc}"
            raise ESLConnectionError(msg) from exc

        sock.settimeout(self._config.timeout)
        self._sock = sock
        self._buffer = b""
        self._connected = True
        self._authenticated = False

        greeting = self._read_frame()
        if greeting.get("Content-Type") != "auth/request":
            self.close()
            msg = f"unexpected ESL greeting: {greeting.get('Content-Type')!r}"
            raise ESLConnectionError(msg)
        log.info("ESL connected: %s:%d", host, port)

    def authenticate(self) -> None:
        """Send the event-socket password and verify acceptance.

        Raises:
            ESLAuthError: If the server rejects the password.
        """
        reply = self._command(f"auth {self._config.password}")
        reply_text = reply.get("Reply-Text", "")
        if not reply_text.startswith("+OK"):
            self.close()
            # Never echo the password; only the server's reason.
            msg = f"ESL auth rejected: {reply_text or 'no reply'}"
            raise ESLAuthError(msg)
        self._authenticated = True
        log.info("ESL authenticated")

    def subscribe(self, events: list[str]) -> None:
        """Subscribe to ``event plain`` for the given event names.

        ``CUSTOM`` classes (e.g. ``callcenter::info``) are requested as
        ``CUSTOM <subclass>``; pass the subclass names in ``events`` and this
        method groups them onto the CUSTOM line.
        """
        plain = [e for e in events if "::" not in e]
        custom = [e for e in events if "::" in e]
        parts = list(plain)
        if custom:
            parts.append("CUSTOM " + " ".join(custom))
        reply = self._command("event plain " + " ".join(parts))
        if not reply.get("Reply-Text", "").startswith("+OK"):
            msg = f"ESL event subscription failed: {reply.get('Reply-Text')}"
            raise ESLError(msg)
        log.debug("ESL subscribed: %s", parts)

    def api(self, command: str, *, allow_unsafe: bool = False) -> str:
        """Run a blocking ``api`` command and return its text response.

        Read-only by default: the complete command must match a sanctioned query
        form unless ``allow_unsafe=True`` (explicit opt-in for a caller that owns
        the risk). Raises :class:`NotSupportedError` otherwise.
        """
        if not allow_unsafe and not _is_read_only_api_command(command):
            msg = (
                f"api command {command!r} is not an allowed read-only form; "
                "pass allow_unsafe=True to run mutating commands"
            )
            raise NotSupportedError(msg)
        frame = self._command(f"api {command}")
        return frame.get("_body", "")

    def close(self) -> None:
        """Close the socket. Idempotent; swallows teardown errors."""
        self._connected = False
        self._authenticated = False
        if self._sock is not None:
            with suppress(OSError):
                self._sock.close()
            self._sock = None

    def __enter__(self) -> ESLClient:  # noqa: PYI034 - package supports Python 3.10
        self.connect()
        try:
            self.authenticate()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def list_registrations(self, profile: str) -> list[SIPRegistration]:
        """Return typed SIP registrations for one validated sofia profile.

        The bare list cannot say *why* it is empty. Use
        ``list_registrations_result`` when the caller needs to know whether an
        empty result is authoritative.
        """
        return self.list_registrations_result(profile).registrations

    def list_registrations_result(self, profile: str) -> SofiaRegResult:
        """Return one profile's registrations plus whether they are complete.

        A caller that acts destructively on an empty list -- marking endpoints
        unregistered, say -- must gate on ``SofiaRegResult.is_authoritative``,
        because an ``-ERR`` refusal and truncated output both parse to zero rows
        and are indistinguishable from a genuinely empty profile.
        """
        if not _is_valid_sip_profile(profile):
            msg = f"invalid FreeSWITCH SIP profile name: {profile!r}"
            raise NotSupportedError(msg)
        text = self.api(f"sofia status profile {profile} reg")
        return parse_sofia_reg_result(text)

    def status(self) -> str:
        """Return the core's read-only status report."""
        return self.api("status")

    def sofia_status(self, profile: str | None = None) -> str:
        """Return global or per-profile Sofia status."""
        if profile is None:
            return self.api("sofia status")
        if not _is_valid_sip_profile(profile):
            msg = f"invalid FreeSWITCH SIP profile name: {profile!r}"
            raise NotSupportedError(msg)
        return self.api(f"sofia status profile {profile}")

    def sofia_xmlstatus(self) -> str:
        """Return global Sofia status in FreeSWITCH's XML form."""
        return self.api("sofia xmlstatus")

    def list_callcenter_queues(self) -> str:
        """Return the read-only mod_callcenter queue listing."""
        return self.api("callcenter_config queue list")

    def list_callcenter_agents(self) -> str:
        """Return the read-only mod_callcenter agent listing."""
        return self.api("callcenter_config agent list")

    def list_callcenter_tiers(self) -> str:
        """Return the read-only mod_callcenter tier listing."""
        return self.api("callcenter_config tier list")

    # ------------------------------------------------------------------
    # Event stream
    # ------------------------------------------------------------------

    def read_event(self) -> dict[str, str]:
        """Read the next event frame (blocking up to the socket timeout).

        Returns the decoded event header map. For ``text/event-plain`` frames the
        outer envelope is unwrapped and the inner event headers are returned
        (with any event body under ``_body``).

        Raises:
            ESLTimeout: No frame arrived within the read window (liveness tick).
            ESLConnectionError: The socket dropped.
            ESLProtocolError: The frame is malformed or exceeds a safety cap.
        """
        outer = self._read_frame()
        ctype = outer.get("Content-Type")
        if ctype == "text/event-plain":
            body = outer.get("_body", "")
            return self._parse_event_body(body)
        # Non-event frames (command replies interleaved, disconnect notices) are
        # surfaced as-is; the listener filters anything without an Event-Name.
        return outer

    # ------------------------------------------------------------------
    # Framing internals
    # ------------------------------------------------------------------

    def _command(self, line: str) -> dict[str, str]:
        """Send one command line and read the immediate reply frame."""
        self._send(line + _END)
        return self._read_frame()

    def _send(self, data: str) -> None:
        if self._sock is None:
            msg = "ESL socket is not connected"
            raise ESLConnectionError(msg)
        try:
            self._sock.sendall(data.encode())
        except OSError as exc:
            self.close()
            msg = f"ESL send failed: {exc}"
            raise ESLConnectionError(msg) from exc

    def _read_frame(self) -> dict[str, str]:
        """Read one header block + optional Content-Length body.

        The body is stored under the ``_body`` key. Header values are left as-is
        here (the outer envelope is ASCII); event-body values are URL-decoded in
        :meth:`_parse_event_body`.
        """
        deadline = time.monotonic() + self._config.timeout
        sock = self._sock
        try:
            header_bytes = self._read_until(
                b"\n\n",
                deadline,
                max_bytes=self._config.max_header_bytes,
            )
            header_size = len(header_bytes) + len(_END)
            if header_size > self._config.max_frame_bytes:
                self._raise_protocol_error(
                    "ESL frame header exceeds the maximum total frame size",
                )

            headers = self._parse_headers(header_bytes.decode(errors="replace"))
            length = headers.get("Content-Length")
            if length is not None:
                n = self._parse_content_length(length, context="outer frame")
                if header_size + n > self._config.max_frame_bytes:
                    self._raise_protocol_error(
                        "ESL outer frame exceeds the maximum total frame size",
                    )
                if n > 0:
                    headers["_body"] = self._read_exact(n, deadline).decode(
                        errors="replace",
                    )
            return headers
        finally:
            if sock is not None and self._sock is sock:
                with suppress(OSError):
                    sock.settimeout(self._config.timeout)

    def _parse_event_body(self, body: str) -> dict[str, str]:
        """Parse a ``text/event-plain`` body into a decoded event header map.

        The body is a header block; header values are URL-encoded. A trailing
        ``Content-Length`` inside the body marks an event *payload* after the
        blank line, preserved under ``_body``.
        """
        head, sep, rest = body.partition(_END)
        header_size = len(head.encode()) + (len(_END) if sep else 0)
        if header_size > self._config.max_header_bytes:
            self._raise_protocol_error(
                "ESL inner event header exceeds the maximum header size",
            )
        if header_size > self._config.max_frame_bytes:
            self._raise_protocol_error(
                "ESL inner event header exceeds the maximum total frame size",
            )

        event = self._parse_headers(head, decode_values=True)
        length = event.get("Content-Length")
        if length is not None:
            n = self._parse_content_length(length, context="inner event payload")
            if header_size + n > self._config.max_frame_bytes:
                self._raise_protocol_error(
                    "ESL inner event exceeds the maximum total frame size",
                )
            if n > 0:
                if not sep or len(rest.encode()) < n:
                    self._raise_protocol_error(
                        "ESL inner event payload is shorter than Content-Length",
                    )
                event["_body"] = rest[:n]
        return event

    def _parse_content_length(self, value: str, *, context: str) -> int:
        """Validate one ASCII-decimal Content-Length against the body cap."""
        if not value.isascii() or not value.isdigit():
            self._raise_protocol_error(
                f"invalid Content-Length for ESL {context}: {value!r}",
            )

        normalized = value.lstrip("0") or "0"
        if len(normalized) > len(str(self._config.max_body_bytes)):
            self._raise_protocol_error(
                f"Content-Length for ESL {context} exceeds the maximum body size",
            )
        # fmt: off
        try:
            length = int(normalized)
        except (TypeError, ValueError):
            self._raise_protocol_error(
                f"invalid Content-Length for ESL {context}: {value!r}",
            )
        # fmt: on
        if length > self._config.max_body_bytes:
            self._raise_protocol_error(
                f"Content-Length for ESL {context} exceeds the maximum body size",
            )
        return length

    @staticmethod
    def _parse_headers(text: str, *, decode_values: bool = False) -> dict[str, str]:
        out: dict[str, str] = {}
        for raw_line in text.split(_LF):
            line = raw_line.rstrip("\r")
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            out[key] = unquote(value) if decode_values else value
        return out

    def _read_until(
        self,
        marker: bytes,
        deadline: float,
        *,
        max_bytes: int,
    ) -> bytes:
        """Read a capped block through ``marker`` within one frame deadline."""
        frame_started = bool(self._buffer)
        while True:
            idx = self._buffer.find(marker)
            if idx >= 0:
                if idx + len(marker) > max_bytes:
                    self._raise_protocol_error(
                        "ESL header block exceeds the maximum header size",
                    )
                block = self._buffer[:idx]
                self._buffer = self._buffer[idx + len(marker) :]
                return block
            if len(self._buffer) >= max_bytes:
                self._raise_protocol_error(
                    "ESL header block exceeds the maximum header size",
                )
            self._buffer += self._recv(deadline, idle_tick=not frame_started)
            frame_started = True

    def _read_exact(self, n: int, deadline: float) -> bytes:
        while len(self._buffer) < n:
            self._buffer += self._recv(deadline, idle_tick=False)
        block = self._buffer[:n]
        self._buffer = self._buffer[n:]
        return block

    def _recv(self, deadline: float, *, idle_tick: bool) -> bytes:
        if self._sock is None:
            msg = "ESL socket is not connected"
            raise ESLConnectionError(msg)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            msg = "ESL frame deadline elapsed before a complete frame arrived"
            raise ESLTimeout(msg)
        try:
            self._sock.settimeout(min(remaining, self._config.timeout))
            chunk = self._sock.recv(4096)
        except socket.timeout as exc:  # noqa: UP041 — socket.timeout for py<3.10 parity
            if idle_tick:
                msg = "ESL read timed out (no frame in the read window)"
            elif time.monotonic() >= deadline:
                msg = "ESL frame deadline elapsed before a complete frame arrived"
            else:
                msg = "ESL read timed out while receiving an incomplete frame"
            raise ESLTimeout(msg) from exc
        except OSError as exc:
            self.close()
            msg = f"ESL read failed: {exc}"
            raise ESLConnectionError(msg) from exc
        if not chunk:
            self.close()
            msg = "ESL connection closed by peer"
            raise ESLConnectionError(msg)
        return chunk

    def _raise_protocol_error(self, message: str) -> NoReturn:
        """Discard unsynchronizable bytes, close the socket, and fail loudly."""
        self._buffer = b""
        self.close()
        raise ESLProtocolError(message)


__all__ = ["ESLClient"]

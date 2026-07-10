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
allow-listed status verbs run through :meth:`api`. Anything else raises
:class:`NotSupportedError` unless the caller passes ``allow_unsafe=True`` — an
explicit, auditable opt-in. The listener path (auth + ``event plain`` subscribe
+ stream) issues no commands at all beyond the subscription.
"""

from __future__ import annotations

import socket
from contextlib import suppress
from urllib.parse import unquote

from pyfreeswitch.config import ESLConfig
from pyfreeswitch.config import normalize_sip_profiles
from pyfreeswitch.exceptions import ESLAuthError
from pyfreeswitch.exceptions import ESLConnectionError
from pyfreeswitch.exceptions import ESLError
from pyfreeswitch.exceptions import ESLTimeout
from pyfreeswitch.exceptions import NotSupportedError
from pyfreeswitch.logging import get_logger
from pyfreeswitch.models.registrations import SIPRegistration
from pyfreeswitch.models.registrations import parse_sofia_reg

log = get_logger("clients.esl")

_LF = "\n"
_END = "\n\n"

# Read-only ``api`` verbs safe for a public library to run directly. The first
# whitespace-delimited token of the command is checked against this set.
_SAFE_API_VERBS: frozenset[str] = frozenset(
    {
        "status",
        "version",
        "uptime",
        "sofia",  # `sofia status`, `sofia status profile <p> reg` (read-only)
        "show",  # `show channels`, `show calls`, `show registrations`
        "callcenter_config",  # `callcenter_config queue list` (read-only sub-verbs)
        "help",
    },
)


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

        Read-only by default: the command's leading verb must be in
        :data:`_SAFE_API_VERBS` unless ``allow_unsafe=True`` (explicit opt-in for
        a caller that owns the risk). Raises :class:`NotSupportedError` otherwise.
        """
        verb = command.strip().split(maxsplit=1)[0] if command.strip() else ""
        if not allow_unsafe and verb not in _SAFE_API_VERBS:
            msg = (
                f"api verb {verb!r} is not in the read-only allow-list; "
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
        """Return typed SIP registrations for one validated sofia profile."""
        if normalize_sip_profiles([profile]) != (profile,):
            msg = f"invalid FreeSWITCH SIP profile name: {profile!r}"
            raise NotSupportedError(msg)
        text = self.api(f"sofia status profile {profile} reg")
        return parse_sofia_reg(text)

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
        header_bytes = self._read_until(b"\n\n")
        headers = self._parse_headers(header_bytes.decode(errors="replace"))
        length = headers.get("Content-Length")
        if length:
            try:
                n = int(length)
            except ValueError:
                n = 0
            if n > 0:
                headers["_body"] = self._read_exact(n).decode(errors="replace")
        return headers

    def _parse_event_body(self, body: str) -> dict[str, str]:
        """Parse a ``text/event-plain`` body into a decoded event header map.

        The body is a header block; header values are URL-encoded. A trailing
        ``Content-Length`` inside the body marks an event *payload* after the
        blank line, preserved under ``_body``.
        """
        head, sep, rest = body.partition(_END)
        event = self._parse_headers(head, decode_values=True)
        if sep and event.get("Content-Length"):
            try:
                n = int(event["Content-Length"])
            except ValueError:
                n = 0
            if n > 0:
                event["_body"] = rest[:n]
        return event

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

    def _read_until(self, marker: bytes) -> bytes:
        """Read until ``marker`` is seen; return bytes up to (excluding) it."""
        while marker not in self._buffer:
            self._buffer += self._recv()
        idx = self._buffer.index(marker)
        block = self._buffer[:idx]
        self._buffer = self._buffer[idx + len(marker) :]
        return block

    def _read_exact(self, n: int) -> bytes:
        while len(self._buffer) < n:
            self._buffer += self._recv()
        block = self._buffer[:n]
        self._buffer = self._buffer[n:]
        return block

    def _recv(self) -> bytes:
        if self._sock is None:
            msg = "ESL socket is not connected"
            raise ESLConnectionError(msg)
        try:
            chunk = self._sock.recv(4096)
        except socket.timeout as exc:  # noqa: UP041 — socket.timeout for py<3.10 parity
            msg = "ESL read timed out (no frame in the read window)"
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


__all__ = ["ESLClient"]

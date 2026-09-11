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

import re
import socket
import time
from collections import deque
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timezone
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
from pyfreeswitch.models.callcenter import AgentState
from pyfreeswitch.models.callcenter import AgentStatus
from pyfreeswitch.models.callcenter import CallCenterAgent
from pyfreeswitch.models.callcenter import CallCenterTier
from pyfreeswitch.models.callcenter import parse_agent_list
from pyfreeswitch.models.callcenter import parse_tier_list
from pyfreeswitch.models.commands import CommandReply
from pyfreeswitch.models.channels import ChannelSnapshot, parse_channel_snapshot
from pyfreeswitch.models.commands import parse_command_reply
from pyfreeswitch.models.registrations import SIPRegistration
from pyfreeswitch.models.registrations import SofiaRegResult
from pyfreeswitch.models.registrations import parse_sofia_reg_result

log = get_logger("clients.esl")

_LF = "\n"
_END = "\n\n"
_MAX_PENDING_EVENTS = 64

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
_SAFE_CALLCENTER_AGENT_GET_KEYS: frozenset[str] = frozenset({"status", "state", "uuid"})

# Identifiers that are interpolated into a space-separated ESL command line.
# Rejecting whitespace, quotes and control characters at the boundary is what
# makes the typed writers below injection-proof; the switch itself does no
# quoting.
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_.@+-]{1,128}$")
_QUEUE_NAME_RE = _AGENT_NAME_RE
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_DEST_EXTEN_RE = re.compile(r"^[A-Za-z0-9_*#+.-]{1,64}$")
_DIALPLAN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_CONTEXT_RE = _DIALPLAN_RE
_HANGUP_CAUSE_RE = re.compile(r"^[A-Z0-9_]{1,64}$")
# ``originate``-style dial strings for att_xfer: ``user/1001``,
# ``sofia/internal/1001@domain`` — a single token, no braces (channel variables
# would let a caller smuggle arbitrary settings), no whitespace.
_DIALSTRING_RE = re.compile(r"^[A-Za-z0-9_./@:+*#-]{1,256}$")
_RECORD_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]{1,512}$")


def _is_valid_sip_profile(profile: str) -> bool:
    return normalize_sip_profiles([profile]) == (profile,)


def _is_agent_name(value: str) -> bool:
    return bool(_AGENT_NAME_RE.fullmatch(value or ""))


def _is_qualified_callcenter_name(value: str) -> bool:
    """Require the canonical ``name@domain`` stored by mod_callcenter.

    FreeSWITCH accepts a short name in a delete command but does not match the
    domain-qualified database row. Requiring the stored form prevents a
    successful-looking cleanup from leaving an orphan behind.
    """
    local, separator, domain = (value or "").partition("@")
    return bool(
        value.count("@") == 1
        and separator
        and local
        and domain
        and _AGENT_NAME_RE.fullmatch(value)
    )


def _is_uuid(value: str) -> bool:
    return bool(_UUID_RE.fullmatch(value or ""))


def _is_read_only_api_command(command: str) -> bool:
    """Return whether the complete command matches a sanctioned query form."""
    parts = command.strip().split()
    allowed = False
    match parts:
        case [bare]:
            allowed = bare in _SAFE_BARE_API_COMMANDS
        case ["show", subcommand]:
            allowed = subcommand in _SAFE_SHOW_SUBCOMMANDS
        case ["show", "channels", "as", "json"]:
            allowed = True
        case ["sofia", query]:
            allowed = query in {"status", "xmlstatus"}
        case ["callcenter_config", target, "list"]:
            allowed = target in _SAFE_CALLCENTER_LIST_TARGETS
        case ["callcenter_config", "agent", "list", agent]:
            allowed = _is_agent_name(agent)
        case ["callcenter_config", "agent", "get", key, agent]:
            allowed = key in _SAFE_CALLCENTER_AGENT_GET_KEYS and _is_agent_name(agent)
        case ["sofia", "status", "profile", profile]:
            allowed = _is_valid_sip_profile(profile)
        case ["sofia", "status", "profile", profile, "reg"]:
            allowed = _is_valid_sip_profile(profile)
    return allowed


def _require(condition: bool, message: str) -> None:  # noqa: FBT001 - guard helper
    if not condition:
        raise NotSupportedError(message)


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
        self._pending_events: deque[tuple[dict[str, str], int]] = deque()
        self._pending_event_bytes = 0

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

        self._sock = sock
        self._buffer = b""
        self._connected = True
        self._authenticated = False
        try:
            sock.settimeout(self._config.timeout)
            # Idle reads are normal after subscription; kernel keepalives can
            # independently detect half-open peers.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            greeting = self._read_frame()
            if greeting.get("Content-Type") != "auth/request":
                msg = f"unexpected ESL greeting: {greeting.get('Content-Type')!r}"
                raise ESLConnectionError(msg)
        except BaseException:
            # __enter__ cannot call __exit__ if connect itself fails. Release
            # the newly opened socket even for greeting timeout/cancellation.
            self.close()
            raise
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
        self._buffer = b""
        self._pending_events.clear()
        self._pending_event_bytes = 0
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

    def list_channels_result(self) -> ChannelSnapshot:
        """Return a typed live-channel snapshot from this configured endpoint.

        Capture time is response receipt, not a server timestamp. A complete
        response is not a transactionally consistent call lifecycle snapshot;
        consumers must reconcile absence with events/CDR, never infer hangup.
        """
        result = parse_channel_snapshot(self.api("show channels as json"))
        return replace(
            result,
            source_host=self._config.host,
            source_port=self._config.port,
            captured_at=datetime.now(timezone.utc),
        )

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
    # mod_callcenter — typed reads
    # ------------------------------------------------------------------

    def list_callcenter_agents_typed(
        self,
        agent: str | None = None,
    ) -> list[CallCenterAgent]:
        """Typed ``callcenter_config agent list [agent]``.

        Raises :class:`ESLError` on an ``-ERR`` reply so an empty list always
        means "no such agents", never "the switch refused".
        """
        if agent is None:
            text = self.api("callcenter_config agent list")
        else:
            _require(_is_agent_name(agent), f"invalid callcenter agent name: {agent!r}")
            text = self.api(f"callcenter_config agent list {agent}")
        try:
            return parse_agent_list(text)
        except ValueError as exc:
            msg = f"callcenter agent list refused: {exc}"
            raise ESLError(msg) from exc

    def list_callcenter_tiers_typed(self) -> list[CallCenterTier]:
        """Typed ``callcenter_config tier list``."""
        text = self.api("callcenter_config tier list")
        try:
            return parse_tier_list(text)
        except ValueError as exc:
            msg = f"callcenter tier list refused: {exc}"
            raise ESLError(msg) from exc

    def get_callcenter_agent_status(self, agent: str) -> AgentStatus:
        """``callcenter_config agent get status <agent>`` → :class:`AgentStatus`.

        The switch prints the bare status text (no ``+OK``) on success and an
        ``-ERR`` line otherwise; the latter is raised as :class:`ESLError`.
        """
        _require(_is_agent_name(agent), f"invalid callcenter agent name: {agent!r}")
        text = self.api(f"callcenter_config agent get status {agent}")
        return AgentStatus.from_text(self._bare_value(text, what="agent status"))

    def get_callcenter_agent_state(self, agent: str) -> AgentState:
        """``callcenter_config agent get state <agent>`` → :class:`AgentState`."""
        _require(_is_agent_name(agent), f"invalid callcenter agent name: {agent!r}")
        text = self.api(f"callcenter_config agent get state {agent}")
        return AgentState.from_text(self._bare_value(text, what="agent state"))

    @staticmethod
    def _bare_value(text: str, *, what: str) -> str:
        stripped = (text or "").strip()
        if stripped.lower().startswith(("-err", "-usage")):
            msg = f"{what} refused: {stripped.splitlines()[0][:200]}"
            raise ESLError(msg)
        return stripped.splitlines()[0] if stripped else ""

    # ------------------------------------------------------------------
    # Typed mutating commands (require ESLConfig.allow_mutations=True)
    # ------------------------------------------------------------------
    #
    # Each method validates every interpolated token, builds the one exact
    # command form documented in mod_callcenter.c / mod_commands.c and passes
    # ``allow_unsafe=True`` itself. Callers never hand this client a raw
    # command string. Replies are typed; the caller reads ``CommandReply.ok``.

    def _mutate(self, command: str) -> CommandReply:
        _require(
            self._config.allow_mutations,
            "this ESL client is read-only (ESLConfig.allow_mutations is False); "
            "construct it with allow_mutations=True to run typed mutating commands",
        )
        log.info("ESL mutation: %s", command.split(" ", 1)[0])
        return parse_command_reply(self.api(command, allow_unsafe=True))

    def set_callcenter_agent_status(
        self,
        agent: str,
        status: AgentStatus,
    ) -> CommandReply:
        """``callcenter_config agent set status <agent> '<status>'``.

        ``Available`` = join, ``Logged Out`` = leave, ``On Break`` = pause. The
        agent and its tiers must already be provisioned; the switch answers
        ``-ERR Agent not found!`` otherwise (``CommandReply.not_found``).
        """
        _require(_is_agent_name(agent), f"invalid callcenter agent name: {agent!r}")
        _require(
            isinstance(status, AgentStatus) and status is not AgentStatus.UNKNOWN,
            f"invalid callcenter agent status: {status!r}",
        )
        return self._mutate(
            f"callcenter_config agent set status {agent} '{status.value}'",
        )

    def set_callcenter_agent_state(
        self,
        agent: str,
        state: AgentState,
    ) -> CommandReply:
        """``callcenter_config agent set state <agent> '<state>'``.

        Normally the switch drives state itself; exposed for reconcile after a
        stuck ``In a queue call`` (set back to ``Waiting``).
        """
        _require(_is_agent_name(agent), f"invalid callcenter agent name: {agent!r}")
        _require(
            isinstance(state, AgentState) and state is not AgentState.UNKNOWN,
            f"invalid callcenter agent state: {state!r}",
        )
        return self._mutate(
            f"callcenter_config agent set state {agent} '{state.value}'",
        )

    def delete_callcenter_tier(self, queue: str, agent: str) -> CommandReply:
        """Idempotently delete one domain-qualified queue/agent tier.

        ``mod_callcenter`` returns ``+OK`` even when the row is already absent.
        Both identifiers must use the canonical ``name@domain`` form returned
        by the typed list methods; short names can otherwise miss the stored row
        while still receiving a successful reply from FreeSWITCH.
        """
        _require(
            _is_qualified_callcenter_name(queue),
            f"invalid or unqualified callcenter queue name: {queue!r}",
        )
        _require(
            _is_qualified_callcenter_name(agent),
            f"invalid or unqualified callcenter agent name: {agent!r}",
        )
        return self._mutate(f"callcenter_config tier del {queue} {agent}")

    def delete_callcenter_agent(self, agent: str) -> CommandReply:
        """Idempotently delete one domain-qualified callcenter agent."""
        _require(
            _is_qualified_callcenter_name(agent),
            f"invalid or unqualified callcenter agent name: {agent!r}",
        )
        return self._mutate(f"callcenter_config agent del {agent}")

    def uuid_kill(self, uuid: str, cause: str | None = None) -> CommandReply:
        """``uuid_kill <uuid> [cause]`` — hang up one channel by UUID."""
        _require(_is_uuid(uuid), f"invalid channel uuid: {uuid!r}")
        if cause is None:
            return self._mutate(f"uuid_kill {uuid}")
        _require(
            bool(_HANGUP_CAUSE_RE.fullmatch(cause)),
            f"invalid hangup cause: {cause!r}",
        )
        return self._mutate(f"uuid_kill {uuid} {cause}")

    def uuid_transfer(
        self,
        uuid: str,
        destination: str,
        *,
        leg: str = "aleg",
        dialplan: str = "XML",
        context: str | None = None,
    ) -> CommandReply:
        """``uuid_transfer <uuid> [-bleg|-both] <dest-exten> [<dialplan>] [<context>]``.

        Blind transfer. ``leg="bleg"`` transfers the *other* party of the bridge
        (the usual "send my caller to X"); ``"both"`` redirects both legs.
        """
        _require(_is_uuid(uuid), f"invalid channel uuid: {uuid!r}")
        # A leading "-" would be re-read by the switch as a leg flag.
        _require(
            bool(_DEST_EXTEN_RE.fullmatch(destination))
            and not destination.startswith("-"),
            f"invalid transfer destination: {destination!r}",
        )
        _require(leg in {"aleg", "bleg", "both"}, f"invalid transfer leg: {leg!r}")
        _require(
            bool(_DIALPLAN_RE.fullmatch(dialplan)), f"invalid dialplan: {dialplan!r}"
        )
        parts = ["uuid_transfer", uuid]
        if leg != "aleg":
            parts.append(f"-{leg}")
        parts.extend([destination, dialplan])
        if context is not None:
            _require(
                bool(_CONTEXT_RE.fullmatch(context)), f"invalid context: {context!r}"
            )
            parts.append(context)
        return self._mutate(" ".join(parts))

    def uuid_attended_transfer(self, uuid: str, dialstring: str) -> CommandReply:
        """Start a warm transfer: ``uuid_broadcast <uuid> att_xfer::<dialstring> aleg``.

        ``att_xfer`` is a dialplan application, not an api command: the
        transferrer's leg (``aleg``) dials ``dialstring`` while the other party
        is held; the transfer *completes* when the transferrer hangs up and is
        *cancelled* when the consultation leg hangs up. There is no separate
        "complete" command — consumers must model that (parity matrix LV row).
        """
        _require(_is_uuid(uuid), f"invalid channel uuid: {uuid!r}")
        _require(
            bool(_DIALSTRING_RE.fullmatch(dialstring)),
            f"invalid att_xfer dial string: {dialstring!r}",
        )
        return self._mutate(f"uuid_broadcast {uuid} att_xfer::{dialstring} aleg")

    def uuid_record(self, uuid: str, action: str, path: str) -> CommandReply:
        """``uuid_record <uuid> start|stop <path>`` — session recording control.

        ``path`` is a switch-local absolute file path; where it lands and how it
        is archived is the core's configuration, not this client's concern.
        """
        _require(_is_uuid(uuid), f"invalid channel uuid: {uuid!r}")
        _require(action in {"start", "stop"}, f"invalid uuid_record action: {action!r}")
        _require(
            bool(_RECORD_PATH_RE.fullmatch(path)) and ".." not in path,
            f"invalid recording path: {path!r}",
        )
        return self._mutate(f"uuid_record {uuid} {action} {path}")

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
        if self._pending_events:
            outer, size = self._pending_events.popleft()
            self._pending_event_bytes -= size
        else:
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
        """Wait for a reply, retaining racing events within finite limits.

        Like upstream libesl's send/recv, command replies and event delivery
        are separate. This synchronous client supports one caller at a time;
        use separate connections for concurrent commands and event consumers.
        A timeout leaves command completion unknown: close, never replay.
        """
        deadline = time.monotonic() + self._config.timeout
        try:
            self._send(line + _END)
            while True:
                if time.monotonic() >= deadline:
                    raise ESLTimeout("ESL command reply deadline elapsed")
                frame = self._read_frame(deadline=deadline)
                ctype = frame.get("Content-Type")
                if ctype in {"api/response", "command/reply"}:
                    return frame
                if ctype != "text/event-plain":
                    self._raise_protocol_error(
                        "unexpected ESL frame while awaiting command reply"
                    )
                size = sum(
                    len(k.encode()) + len(v.encode()) + 4 for k, v in frame.items()
                )
                if (
                    len(self._pending_events) >= _MAX_PENDING_EVENTS
                    or self._pending_event_bytes + size > self._config.max_frame_bytes
                ):
                    self._raise_protocol_error(
                        "ESL pending event buffer exceeds its limit"
                    )
                self._pending_events.append((frame, size))
                self._pending_event_bytes += size
        except ESLTimeout:
            self.close()
            raise

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

    def _read_frame(self, *, deadline: float | None = None) -> dict[str, str]:
        """Read one header block + optional Content-Length body.

        The body is stored under the ``_body`` key. Header values are left as-is
        here (the outer envelope is ASCII); event-body values are URL-decoded in
        :meth:`_parse_event_body`.
        """
        if deadline is None:
            deadline = time.monotonic() + self._config.timeout
        sock = self._sock
        header_consumed = False
        try:
            header_bytes = self._read_until(
                b"\n\n",
                deadline,
                max_bytes=self._config.max_header_bytes,
            )
            header_consumed = True
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
        except ESLTimeout as exc:
            if header_consumed or self._buffer:
                # Header/body bytes cannot be interpreted as a new frame after
                # an idle tick. Force the listener's existing reconnect path.
                self.close()
                raise ESLConnectionError(
                    "ESL timed out receiving an incomplete frame"
                ) from exc
            raise
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

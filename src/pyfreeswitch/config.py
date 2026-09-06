"""Connection and authentication settings for the FreeSWITCH event socket."""

from __future__ import annotations

import math
import re
from typing import Any

from pydantic import Field
from pydantic import field_validator
from pydantic_settings import BaseSettings

DEFAULT_ESL_TIMEOUT_SECONDS = 10.0
MIN_ESL_TIMEOUT_SECONDS = 1.0
MAX_ESL_TIMEOUT_SECONDS = 30.0
DEFAULT_ESL_MAX_HEADER_BYTES = 64 * 1024
MIN_ESL_MAX_HEADER_BYTES = 1024
MAX_ESL_MAX_HEADER_BYTES = 1024 * 1024
DEFAULT_ESL_MAX_BODY_BYTES = 1024 * 1024
MIN_ESL_MAX_BODY_BYTES = 1024
MAX_ESL_MAX_BODY_BYTES = 16 * 1024 * 1024
DEFAULT_ESL_MAX_FRAME_BYTES = DEFAULT_ESL_MAX_HEADER_BYTES + DEFAULT_ESL_MAX_BODY_BYTES
MIN_ESL_MAX_FRAME_BYTES = MIN_ESL_MAX_HEADER_BYTES + MIN_ESL_MAX_BODY_BYTES
MAX_ESL_MAX_FRAME_BYTES = MAX_ESL_MAX_HEADER_BYTES + MAX_ESL_MAX_BODY_BYTES
DEFAULT_SIP_PROFILES = ("wg", "vpc")
MAX_SIP_PROFILES = 4
_SIP_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def normalize_sip_profiles(value: object | None) -> tuple[str, ...]:
    """Return unique, command-safe FreeSWITCH profile names within a hard cap."""
    if value is None:
        return DEFAULT_SIP_PROFILES
    if not isinstance(value, (list, tuple)):
        return ()

    profiles: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _SIP_PROFILE_RE.fullmatch(item):
            continue
        if item not in profiles:
            profiles.append(item)
        if len(profiles) == MAX_SIP_PROFILES:
            break
    return tuple(profiles)


class ESLConfig(BaseSettings):
    """Configuration for a FreeSWITCH inbound Event Socket Layer connection.

    Inbound mode: the client opens a TCP connection to ``mod_event_socket`` and
    authenticates with the event-socket password (``event_socket.conf.xml``).
    This is the only mode this library implements — the UniqueOS core runs the
    socket bound to the prod-VPC ENI, reached over the transport tunnel.
    """

    model_config = {"env_prefix": "ESL_"}

    host: str = Field(description="FreeSWITCH host or IP running mod_event_socket")
    port: int = Field(default=8021, description="Event-socket TCP port")
    password: str = Field(description="Event-socket password (auth)")
    timeout: float = Field(
        default=DEFAULT_ESL_TIMEOUT_SECONDS,
        description="Socket read timeout in seconds; also the idle-tick window.",
    )
    max_header_bytes: int = Field(
        default=DEFAULT_ESL_MAX_HEADER_BYTES,
        description="Maximum ESL header block size, including its terminator.",
    )
    max_body_bytes: int = Field(
        default=DEFAULT_ESL_MAX_BODY_BYTES,
        description="Maximum declared outer or inner ESL body size.",
    )
    max_frame_bytes: int = Field(
        default=DEFAULT_ESL_MAX_FRAME_BYTES,
        description="Maximum total ESL frame size (header plus declared body).",
    )
    allow_mutations: bool = Field(
        default=False,
        description=(
            "Permit the typed mutating commands (callcenter agent status/state, "
            "uuid_kill, uuid_transfer, att_xfer, uuid_record). Off by default: a "
            "listener or read-only probe must never be able to control the switch."
        ),
    )

    @field_validator("timeout", mode="before")
    @classmethod
    def normalize_timeout(cls, value: Any) -> float:
        """Keep all public SDK clients on a finite socket-timeout budget."""
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            return DEFAULT_ESL_TIMEOUT_SECONDS
        if not math.isfinite(timeout):
            return DEFAULT_ESL_TIMEOUT_SECONDS
        return min(
            max(timeout, MIN_ESL_TIMEOUT_SECONDS),
            MAX_ESL_TIMEOUT_SECONDS,
        )

    @field_validator("max_header_bytes", mode="before")
    @classmethod
    def normalize_max_header_bytes(cls, value: Any) -> int:
        """Keep ESL header buffering within finite, conservative bounds."""
        # fmt: off
        try:
            size = float(value)
        except (TypeError, ValueError):
            return DEFAULT_ESL_MAX_HEADER_BYTES
        # fmt: on
        if not math.isfinite(size):
            return DEFAULT_ESL_MAX_HEADER_BYTES
        return int(
            min(
                max(size, MIN_ESL_MAX_HEADER_BYTES),
                MAX_ESL_MAX_HEADER_BYTES,
            ),
        )

    @field_validator("max_body_bytes", mode="before")
    @classmethod
    def normalize_max_body_bytes(cls, value: Any) -> int:
        """Keep ESL body buffering within finite, conservative bounds."""
        # fmt: off
        try:
            size = float(value)
        except (TypeError, ValueError):
            return DEFAULT_ESL_MAX_BODY_BYTES
        # fmt: on
        if not math.isfinite(size):
            return DEFAULT_ESL_MAX_BODY_BYTES
        return int(
            min(
                max(size, MIN_ESL_MAX_BODY_BYTES),
                MAX_ESL_MAX_BODY_BYTES,
            ),
        )

    @field_validator("max_frame_bytes", mode="before")
    @classmethod
    def normalize_max_frame_bytes(cls, value: Any) -> int:
        """Keep total ESL frame buffering within finite, conservative bounds."""
        # fmt: off
        try:
            size = float(value)
        except (TypeError, ValueError):
            return DEFAULT_ESL_MAX_FRAME_BYTES
        # fmt: on
        if not math.isfinite(size):
            return DEFAULT_ESL_MAX_FRAME_BYTES
        return int(
            min(
                max(size, MIN_ESL_MAX_FRAME_BYTES),
                MAX_ESL_MAX_FRAME_BYTES,
            ),
        )

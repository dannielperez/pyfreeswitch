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

    @field_validator("timeout", mode="before")
    @classmethod
    def normalize_timeout(cls, value: Any) -> float:
        """Keep all public SDK clients on a finite socket-timeout budget."""
        try:
            timeout = float(value)
        except TypeError, ValueError:
            return DEFAULT_ESL_TIMEOUT_SECONDS
        if not math.isfinite(timeout):
            return DEFAULT_ESL_TIMEOUT_SECONDS
        return min(
            max(timeout, MIN_ESL_TIMEOUT_SECONDS),
            MAX_ESL_TIMEOUT_SECONDS,
        )

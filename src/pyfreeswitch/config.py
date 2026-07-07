"""Connection and authentication settings for the FreeSWITCH event socket."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


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
        default=10.0,
        description="Socket read timeout in seconds; also the idle-tick window.",
    )

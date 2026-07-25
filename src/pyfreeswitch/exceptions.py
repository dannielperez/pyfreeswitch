"""Exception hierarchy for pyfreeswitch."""

from __future__ import annotations


class FreeSwitchError(Exception):
    """Base exception for all pyfreeswitch errors."""


class ConfigError(FreeSwitchError):
    """Missing or invalid configuration."""


class ESLError(FreeSwitchError):
    """Error from the FreeSWITCH Event Socket Layer."""


class ESLConnectionError(ESLError):
    """Failed to connect to the event socket."""


class ESLAuthError(ESLError):
    """Event-socket authentication failed (bad password / auth denied)."""


class ESLTimeout(ESLError):
    """A blocking read/command exceeded its deadline.

    Distinct from a dropped socket: the connection is still open but no frame
    arrived in the read window. The listener surfaces this as a liveness tick,
    never as a real event.
    """


class ESLProtocolError(ESLConnectionError):
    """Connection-fatal ESL framing violation.

    The socket has already been closed when this is raised; the caller must
    reconnect.
    """


class NotSupportedError(FreeSwitchError):
    """Operation is not supported / not allow-listed on this client.

    Raised when a caller asks for an ESL command outside the safe allow-list.
    Prefer this over silently running an arbitrary administrative command.
    """

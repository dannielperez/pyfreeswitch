"""Synchronous FreeSWITCH ESL event listener.

Connects to the event socket, authenticates, subscribes to ``event plain``, and
streams **typed events**. Synchronous and stateless by design:

* Reuses the :class:`~pyfreeswitch.clients.esl.ESLClient` socket transport (no
  asyncio, no new runtime dependencies).
* Holds **no** call-session state and **no** leg map — those belong to the
  consuming application.
* A consumer needing async (e.g. a long-lived runner) bridges this with a
  thread + queue; the listener itself just yields events.

Subscription is scoped to the call-signalling events the pipeline needs; the
noise filter drops the high-volume media/state events FreeSWITCH emits.
"""

from __future__ import annotations

import time
from contextlib import suppress
from typing import TYPE_CHECKING

from pyfreeswitch.clients.esl import ESLClient
from pyfreeswitch.clients.esl_parser import parse_event
from pyfreeswitch.exceptions import ESLConnectionError
from pyfreeswitch.exceptions import ESLError
from pyfreeswitch.exceptions import ESLTimeout
from pyfreeswitch.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator

    from pyfreeswitch.config import ESLConfig
    from pyfreeswitch.models.events import ESLEvent

log = get_logger("clients.esl_listener")


class _IdleTick:
    """Transport-only sentinel: one read window elapsed, socket alive, no frame.

    NOT an :class:`~pyfreeswitch.models.events.ESLEvent` — a liveness signal
    only, yielded by :meth:`ESLEventListener.iter_raw`/:meth:`listen` on an idle
    read timeout. Compared by identity (``is ESL_IDLE``); never parsed.
    """

    __slots__ = ()


ESL_IDLE = _IdleTick()


# The call-signalling events the UniqueOS pipeline subscribes to. CUSTOM classes
# use the ``module::subclass`` form (grouped onto the CUSTOM line by the client).
DEFAULT_EVENTS: tuple[str, ...] = (
    "CHANNEL_CREATE",
    "CHANNEL_ANSWER",
    "CHANNEL_BRIDGE",
    "CHANNEL_HANGUP_COMPLETE",
    "callcenter::info",
)


class ESLEventListener:
    """Stream typed FreeSWITCH events from one core.

    Usage::

        listener = ESLEventListener(config)
        listener.run_forever(lambda ev: handle(ev))   # blocks; reconnects

    or, for one connection's worth of events::

        listener.start()
        for event in listener.listen():
            if event is ESL_IDLE:       # transport liveness tick, not an event
                continue
            handle(event)
    """

    def __init__(
        self,
        config: ESLConfig,
        *,
        client: ESLClient | None = None,
        events: tuple[str, ...] = DEFAULT_EVENTS,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Args:
        config: ESL connection settings.
        client: Optional pre-built client (tests inject one; else created).
        events: Event names to subscribe to.
        clock: Arrival-time source (epoch seconds); injectable for tests.
        sleep: Backoff sleep function; injectable for tests.
        """
        self._config = config
        self._client = client if client is not None else ESLClient(config)
        self._events = events
        self._clock = clock
        self._sleep = sleep
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Connect, authenticate, and subscribe (events flow immediately after)."""
        self._client.connect()
        self._client.authenticate()
        self._client.subscribe(list(self._events))

    def close(self) -> None:
        """Stop the listener and release the connection. Idempotent."""
        self._closed = True
        with suppress(ESLError, OSError):
            self._client.close()

    def __enter__(self) -> ESLEventListener:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def iter_raw(self) -> Iterator[dict[str, str] | _IdleTick]:
        """Yield raw ESL event frames for one connection.

        Skips frames with no ``Event-Name`` header (interleaved command replies,
        disconnect notices). Raises :class:`ESLConnectionError` on a drop (the
        caller decides whether to reconnect). On an idle read timeout yields the
        :data:`ESL_IDLE` sentinel — a liveness tick, never call data.
        """
        while not self._closed:
            try:
                frame = self._client.read_event()
            except ESLTimeout:
                yield ESL_IDLE
                continue
            if "Event-Name" not in frame:
                continue
            yield frame

    def listen(self) -> Iterator[ESLEvent | _IdleTick]:
        """Yield typed events for one connection (call :meth:`start` first).

        Passes the :data:`ESL_IDLE` transport sentinel through untouched; only
        real frames are parsed. Consumers must filter ``ESL_IDLE``.
        """
        for item in self.iter_raw():
            if isinstance(item, _IdleTick):
                yield item
            else:
                yield parse_event(item, self._clock())

    def run_forever(
        self,
        on_event: Callable[[ESLEvent], None],
        *,
        base_backoff: float = 1.0,
        max_backoff: float = 30.0,
    ) -> None:
        """Stream events to ``on_event`` forever, reconnecting with backoff.

        Returns only after :meth:`close`. A dropped connection is caught and
        retried with exponential backoff (reset on each clean connect) so the
        loop degrades gracefully rather than crashing on a transient drop.
        """
        backoff = base_backoff
        while not self._closed:
            try:
                self.start()
                backoff = base_backoff
                for event in self.listen():
                    if isinstance(event, _IdleTick):
                        continue
                    on_event(event)
            except (ESLConnectionError, OSError) as exc:
                if self._closed:
                    return
                log.warning("ESL connection lost: %s; retry in %.1fs", exc, backoff)
                self._sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            finally:
                self._client.close()


__all__ = ["DEFAULT_EVENTS", "ESL_IDLE", "ESLEventListener"]

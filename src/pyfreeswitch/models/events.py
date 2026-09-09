"""Typed FreeSWITCH ESL event DTOs for the event listener.

Faithful, **stateless** representations of the ``event plain`` frames the
telephony pipeline consumes. Parsing/normalization lives in
:mod:`pyfreeswitch.clients.esl_parser`; correlation *state* (call sessions,
leg maps) belongs to the consuming application, never here.

Correlation fields (FreeSWITCH channel model):

* ``call_uuid`` (``Channel-Call-UUID``) is FreeSWITCH's channel call UUID. In
  common dialplans it equals ``unique_id`` and is **not** guaranteed to be
  stable across bridged or loopback legs.
* ``unique_id`` (``Unique-ID``) is per-channel (per-leg).
* ``variable_uuid`` and ``variable_call_uuid`` preserve the corresponding
  channel variables separately; they often repeat the leg identity, but the
  SDK never assumes that they do.
* ``originating_leg_uuid``, ``signal_bond`` and
  ``other_loopback_leg_uuid`` are explicit relationships to another channel;
  the consuming application decides whether those legs form one domain call.
* Both are read from the frame when present and left ``None`` otherwise —
  **never fabricated**; the consumer resolves any gap.
* ``hangup_cause`` is metadata only; call *disposition* is derived from the
  event sequence by the consumer, not from the cause string.
* Every unmodelled frame becomes :class:`UnknownEvent`, carrying the full
  ``raw`` header map (faithful passthrough, never dropped).
"""

from __future__ import annotations

from pydantic import BaseModel
from pydantic import Field


class ESLEvent(BaseModel):
    """Base for every parsed FreeSWITCH event.

    ``raw`` retains the complete original header map so consumers can read
    fields this library has not promoted to typed attributes.
    """

    event_name: str
    received_at: float
    unique_id: str | None = None
    call_uuid: str | None = None
    variable_uuid: str | None = None
    variable_call_uuid: str | None = None
    channel_name: str | None = None
    originating_leg_uuid: str | None = None
    signal_bond: str | None = None
    other_loopback_leg_uuid: str | None = None
    loopback_leg: str | None = None
    sip_call_id: str | None = None
    origination_uuid: str | None = None
    raw: dict[str, str] = Field(default_factory=dict)


class UnknownEvent(ESLEvent):
    """Any FreeSWITCH event the parser does not model (RTP/media/state noise)."""


# ----------------------------------------------------------------------
# Channel lifecycle
# ----------------------------------------------------------------------


class ChannelCreateEvent(ESLEvent):
    """A channel was created — ``CHANNEL_CREATE``."""

    direction: str | None = None
    caller_id_number: str | None = None
    caller_id_name: str | None = None
    destination_number: str | None = None
    context: str | None = None


class ChannelAnswerEvent(ESLEvent):
    """A channel was answered — ``CHANNEL_ANSWER`` (two-way media begins)."""

    answer_state: str | None = None
    caller_id_number: str | None = None
    destination_number: str | None = None


class ChannelBridgeEvent(ESLEvent):
    """Two channels were bridged — ``CHANNEL_BRIDGE`` (call connected)."""

    other_leg_unique_id: str | None = None
    bridge_a_unique_id: str | None = None
    bridge_b_unique_id: str | None = None


class ChannelHangupEvent(ESLEvent):
    """A channel finished tearing down — ``CHANNEL_HANGUP_COMPLETE``.

    ``hangup_cause`` is **metadata only** — a normal answered hangup and an
    abandoned queue call can both report ``NORMAL_CLEARING``. ``billsec`` is the
    billable (answered) seconds; ``duration`` is total channel seconds.
    """

    hangup_cause: str | None = None
    duration: int | None = None
    billsec: int | None = None
    sip_from_user: str | None = None
    sip_to_user: str | None = None


# ----------------------------------------------------------------------
# mod_callcenter (ACD queues) — CUSTOM / callcenter::info
# ----------------------------------------------------------------------


class CallCenterEvent(ESLEvent):
    """A ``mod_callcenter`` ACD event — ``CUSTOM`` / ``callcenter::info``.

    ``cc_action`` is the sub-type (``members-count``, ``member-queue-start``,
    ``member-queue-end``, ``agent-offering``, ``bridge-agent-start``,
    ``bridge-agent-end``, ``member-queue-resume`` …). Queue/agent identity plus
    the member's channel UUID let the consumer stitch ACD legs to a call.
    """

    cc_action: str | None = None
    cc_queue: str | None = None
    cc_agent: str | None = None
    cc_member_uuid: str | None = None
    cc_member_session_uuid: str | None = None
    cc_agent_uuid: str | None = None
    cc_cause: str | None = None
    cc_agent_called_time: int | None = None
    cc_agent_answered_time: int | None = None
    cc_agent_aborted_time: int | None = None
    cc_member_joined_time: int | None = None
    cc_member_leaving_time: int | None = None
    cc_bridge_terminated_time: int | None = None


__all__ = [
    "CallCenterEvent",
    "ChannelAnswerEvent",
    "ChannelBridgeEvent",
    "ChannelCreateEvent",
    "ChannelHangupEvent",
    "ESLEvent",
    "UnknownEvent",
]

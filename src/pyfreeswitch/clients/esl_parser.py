"""Pure FreeSWITCH-frame -> typed-DTO parsing for the event listener.

A single pure function, :func:`parse_event`, maps one parsed ESL frame (a
``{header: value}`` dict, as produced by the ESL transport) to a typed
:class:`~pyfreeswitch.models.events.ESLEvent`. It has **no I/O and no state** —
all correlation state belongs to the consumer.

Normalization rules (see :mod:`pyfreeswitch.models.events`):

* Channel identity and relationship headers are read when present and left
  ``None`` otherwise — **never fabricated**.
* ``CUSTOM`` frames dispatch on ``Event-Subclass`` (e.g. ``callcenter::info``).
* Frames whose event name is not modelled become :class:`UnknownEvent`,
  carrying the full ``raw`` frame (faithful passthrough, never dropped).
"""

from __future__ import annotations

from pyfreeswitch.models.events import CallCenterEvent
from pyfreeswitch.models.events import ChannelAnswerEvent
from pyfreeswitch.models.events import ChannelBridgeEvent
from pyfreeswitch.models.events import ChannelCreateEvent
from pyfreeswitch.models.events import ChannelHangupEvent
from pyfreeswitch.models.events import ESLEvent
from pyfreeswitch.models.events import UnknownEvent

# Event name -> (DTO class, {dto_attribute: FreeSWITCH header}). Base attributes
# (unique_id/call_uuid/channel_name) are mapped generically, NOT repeated here.
_FIELD_MAP: dict[str, tuple[type[ESLEvent], dict[str, str]]] = {
    "CHANNEL_CREATE": (
        ChannelCreateEvent,
        {
            "direction": "Call-Direction",
            "caller_id_number": "Caller-Caller-ID-Number",
            "caller_id_name": "Caller-Caller-ID-Name",
            "destination_number": "Caller-Destination-Number",
            "context": "Caller-Context",
        },
    ),
    "CHANNEL_ANSWER": (
        ChannelAnswerEvent,
        {
            "answer_state": "Answer-State",
            "caller_id_number": "Caller-Caller-ID-Number",
            "destination_number": "Caller-Destination-Number",
        },
    ),
    "CHANNEL_BRIDGE": (
        ChannelBridgeEvent,
        {
            "other_leg_unique_id": "Other-Leg-Unique-ID",
            "bridge_a_unique_id": "Bridge-A-Unique-ID",
            "bridge_b_unique_id": "Bridge-B-Unique-ID",
        },
    ),
    "CHANNEL_HANGUP_COMPLETE": (
        ChannelHangupEvent,
        {
            "hangup_cause": "Hangup-Cause",
            "duration": "variable_duration",
            "billsec": "variable_billsec",
            "sip_from_user": "variable_sip_from_user",
            "sip_to_user": "variable_sip_to_user",
        },
    ),
    # CUSTOM/callcenter::info sub-dispatch (see _subclass_key).
    "callcenter::info": (
        CallCenterEvent,
        {
            "cc_action": "CC-Action",
            "cc_queue": "CC-Queue",
            "cc_agent": "CC-Agent",
            "cc_member_uuid": "CC-Member-Session-UUID",
            "cc_cause": "CC-Cause",
        },
    ),
}

# Integer-typed DTO attributes: coerce "" / non-numeric to None so pydantic's
# int | None fields accept a quiet/empty header.
_INT_ATTRS: frozenset[str] = frozenset({"duration", "billsec"})


def _dispatch_key(frame: dict[str, str]) -> str:
    """The map key for a frame: Event-Subclass for CUSTOM, else Event-Name."""
    name = frame.get("Event-Name", "")
    if name == "CUSTOM":
        return frame.get("Event-Subclass", "CUSTOM")
    return name


def _coerce_int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_event(frame: dict[str, str], received_at: float) -> ESLEvent:
    """Map one parsed ESL frame to a typed event (pure; no I/O, no state)."""
    base = {
        "event_name": frame.get("Event-Name", ""),
        "received_at": received_at,
        "unique_id": frame.get("Unique-ID") or None,
        "call_uuid": frame.get("Channel-Call-UUID") or None,
        "variable_uuid": frame.get("variable_uuid") or None,
        "variable_call_uuid": frame.get("variable_call_uuid") or None,
        "channel_name": frame.get("Channel-Name") or None,
        "originating_leg_uuid": frame.get("variable_originating_leg_uuid")
        or frame.get("Originating-Leg-UUID")
        or None,
        "signal_bond": frame.get("variable_signal_bond") or None,
        "other_loopback_leg_uuid": frame.get("variable_other_loopback_leg_uuid")
        or None,
        "loopback_leg": frame.get("variable_loopback_leg") or None,
        "sip_call_id": frame.get("variable_sip_call_id") or None,
        "origination_uuid": frame.get("variable_origination_uuid") or None,
        "raw": frame,
    }

    entry = _FIELD_MAP.get(_dispatch_key(frame))
    if entry is None:
        return UnknownEvent(**base)

    dto_cls, field_map = entry
    extra: dict[str, object] = {}
    for attr, header in field_map.items():
        value = frame.get(header)
        extra[attr] = _coerce_int(value) if attr in _INT_ATTRS else value
    return dto_cls(**base, **extra)


__all__ = ["parse_event"]

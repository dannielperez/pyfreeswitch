"""Frame -> typed event parsing (pure, no I/O)."""

from __future__ import annotations

from pyfreeswitch.clients.esl_parser import parse_event
from pyfreeswitch.models.events import (
    CallCenterEvent,
    ChannelCreateEvent,
    ChannelHangupEvent,
    UnknownEvent,
)


def test_channel_create_maps_caller_fields() -> None:
    frame = {
        "Event-Name": "CHANNEL_CREATE",
        "Unique-ID": "leg-1",
        "Channel-Call-UUID": "call-1",
        "Channel-Name": "sofia/wg/101@core",
        "Call-Direction": "inbound",
        "Caller-Caller-ID-Number": "1799",
        "Caller-Destination-Number": "99",
        "Caller-Context": "intercom",
    }
    event = parse_event(frame, received_at=123.0)
    assert isinstance(event, ChannelCreateEvent)
    assert event.call_uuid == "call-1"
    assert event.unique_id == "leg-1"
    assert event.caller_id_number == "1799"
    assert event.destination_number == "99"
    assert event.context == "intercom"
    assert event.received_at == 123.0


def test_channel_relationship_fields_are_promoted_without_inventing_identity() -> None:
    frame = {
        "Event-Name": "CHANNEL_CREATE",
        "Unique-ID": "loop-b",
        "Channel-Call-UUID": "loop-b",
        "variable_uuid": "variable-loop-b",
        "variable_call_uuid": "variable-call-b",
        "variable_originating_leg_uuid": "root-a",
        "variable_signal_bond": "peer-c",
        "variable_other_loopback_leg_uuid": "loop-a",
        "variable_loopback_leg": "B",
        "variable_sip_call_id": "dialog@example.test",
        "variable_origination_uuid": "originate-root",
    }

    event = parse_event(frame, received_at=1.0)

    assert event.originating_leg_uuid == "root-a"
    assert event.variable_uuid == "variable-loop-b"
    assert event.variable_call_uuid == "variable-call-b"
    assert event.signal_bond == "peer-c"
    assert event.other_loopback_leg_uuid == "loop-a"
    assert event.loopback_leg == "B"
    assert event.sip_call_id == "dialog@example.test"
    assert event.origination_uuid == "originate-root"


def test_missing_channel_relationship_fields_stay_none() -> None:
    event = parse_event(
        {"Event-Name": "CHANNEL_CREATE", "Unique-ID": "standalone"},
        received_at=1.0,
    )

    assert event.originating_leg_uuid is None
    assert event.variable_uuid is None
    assert event.variable_call_uuid is None
    assert event.signal_bond is None
    assert event.other_loopback_leg_uuid is None
    assert event.loopback_leg is None
    assert event.sip_call_id is None
    assert event.origination_uuid is None


def test_hangup_coerces_int_billsec_and_duration() -> None:
    frame = {
        "Event-Name": "CHANNEL_HANGUP_COMPLETE",
        "Channel-Call-UUID": "call-2",
        "Hangup-Cause": "NORMAL_CLEARING",
        "variable_duration": "42",
        "variable_billsec": "37",
        "variable_sip_from_user": "101",
        "variable_sip_to_user": "99",
    }
    event = parse_event(frame, received_at=1.0)
    assert isinstance(event, ChannelHangupEvent)
    assert event.duration == 42
    assert event.billsec == 37
    assert event.hangup_cause == "NORMAL_CLEARING"


def test_hangup_empty_billsec_becomes_none() -> None:
    frame = {"Event-Name": "CHANNEL_HANGUP_COMPLETE", "variable_billsec": ""}
    event = parse_event(frame, received_at=1.0)
    assert isinstance(event, ChannelHangupEvent)
    assert event.billsec is None


def test_custom_callcenter_dispatches_on_subclass() -> None:
    frame = {
        "Event-Name": "CUSTOM",
        "Event-Subclass": "callcenter::info",
        "CC-Action": "bridge-agent-start",
        "CC-Queue": "99@core",
        "CC-Agent": "agent-101",
        "CC-Member-Session-UUID": "member-1",
    }
    event = parse_event(frame, received_at=1.0)
    assert isinstance(event, CallCenterEvent)
    assert event.cc_action == "bridge-agent-start"
    assert event.cc_queue == "99@core"
    assert event.cc_member_uuid == "member-1"


def test_unmodelled_event_is_unknown_but_faithful() -> None:
    frame = {"Event-Name": "RECV_RTCP_MESSAGE", "Unique-ID": "x"}
    event = parse_event(frame, received_at=1.0)
    assert isinstance(event, UnknownEvent)
    assert event.event_name == "RECV_RTCP_MESSAGE"
    assert event.raw == frame

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
        "variable_cc_queue": "99@core",
        "variable_cc_agent": "agent-101@core",
        "variable_cc_member_uuid": "member-stable-1",
        "variable_cc_member_session_uuid": "member-session-1",
        "variable_cc_agent_uuid": "agent-leg-1",
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
    assert event.cc_queue == "99@core"
    assert event.cc_agent == "agent-101@core"
    assert event.cc_member_uuid == "member-stable-1"
    assert event.cc_member_session_uuid == "member-session-1"
    assert event.cc_agent_uuid == "agent-leg-1"


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
    assert event.cc_queue is None
    assert event.cc_agent is None
    assert event.cc_member_uuid is None
    assert event.cc_member_session_uuid is None
    assert event.cc_agent_uuid is None


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
        "CC-Agent-UUID": "agent-leg-1",
        "CC-Member-UUID": "member-stable-1",
        "CC-Member-Session-UUID": "member-1",
        "CC-Agent-Called-Time": "1788955201",
        "CC-Agent-Answered-Time": "1788955203",
        "CC-Member-Joined-Time": "1788955200",
    }
    event = parse_event(frame, received_at=1.0)
    assert isinstance(event, CallCenterEvent)
    assert event.cc_action == "bridge-agent-start"
    assert event.cc_queue == "99@core"
    assert event.cc_member_uuid == "member-stable-1"
    assert event.cc_member_session_uuid == "member-1"
    assert event.cc_agent_uuid == "agent-leg-1"
    assert event.cc_agent_called_time == 1788955201
    assert event.cc_agent_answered_time == 1788955203
    assert event.cc_member_joined_time == 1788955200


def test_callcenter_join_epoch_accepts_channel_variable_spelling() -> None:
    frame = {
        "Event-Name": "CUSTOM",
        "Event-Subclass": "callcenter::info",
        "CC-Action": "member-queue-start",
        "CC-Queue": "99@core",
        "CC-Member-UUID": "member-stable-1",
        "CC-Member-Session-UUID": "member-1",
        "variable_cc_queue_joined_epoch": "1788955200",
    }

    event = parse_event(frame, received_at=1788955200.875)

    assert isinstance(event, CallCenterEvent)
    assert event.cc_member_joined_time == 1788955200


def test_unmodelled_event_is_unknown_but_faithful() -> None:
    frame = {"Event-Name": "RECV_RTCP_MESSAGE", "Unique-ID": "x"}
    event = parse_event(frame, received_at=1.0)
    assert isinstance(event, UnknownEvent)
    assert event.event_name == "RECV_RTCP_MESSAGE"
    assert event.raw == frame


# ----------------------------------------------------------------------
# FreeSWITCH event identity (Core-UUID / Event-Sequence / Event-Date-Timestamp)
# ----------------------------------------------------------------------


def test_event_identity_headers_are_promoted_on_modelled_events() -> None:
    frame = {
        "Event-Name": "CHANNEL_CREATE",
        "Unique-ID": "leg-1",
        "Core-UUID": "6f1b2c3d-0000-4000-8000-000000000001",
        "Event-Sequence": "48213",
        "Event-Date-Timestamp": "1764000000123456",
    }

    event = parse_event(frame, received_at=1.0)

    assert isinstance(event, ChannelCreateEvent)
    assert event.core_uuid == "6f1b2c3d-0000-4000-8000-000000000001"
    assert event.event_sequence == 48213
    assert event.event_timestamp_us == 1764000000123456


def test_event_identity_headers_are_promoted_on_unknown_and_custom_events() -> None:
    unknown = parse_event(
        {
            "Event-Name": "CHANNEL_CALLSTATE",
            "Core-UUID": "core-a",
            "Event-Sequence": "7",
            "Event-Date-Timestamp": "1764000000000001",
        },
        received_at=1.0,
    )
    custom = parse_event(
        {
            "Event-Name": "CUSTOM",
            "Event-Subclass": "callcenter::info",
            "Core-UUID": "core-a",
            "Event-Sequence": "8",
        },
        received_at=1.0,
    )

    assert isinstance(unknown, UnknownEvent)
    assert (unknown.core_uuid, unknown.event_sequence) == ("core-a", 7)
    assert unknown.event_timestamp_us == 1764000000000001
    assert isinstance(custom, CallCenterEvent)
    assert (custom.core_uuid, custom.event_sequence) == ("core-a", 8)
    assert custom.event_timestamp_us is None


def test_missing_event_identity_headers_stay_none() -> None:
    event = parse_event({"Event-Name": "CHANNEL_CREATE"}, received_at=1.0)

    assert event.core_uuid is None
    assert event.event_sequence is None
    assert event.event_timestamp_us is None


def test_malformed_event_identity_headers_are_none_never_fabricated() -> None:
    for bad in ("", "   ", "-1", "+5", "1.5", "1e3", "abc", "12 34", "١٢"):
        event = parse_event(
            {
                "Event-Name": "CHANNEL_CREATE",
                "Core-UUID": "core-a",
                "Event-Sequence": bad,
                "Event-Date-Timestamp": bad,
            },
            received_at=1.0,
        )
        assert event.event_sequence is None, bad
        assert event.event_timestamp_us is None, bad
        assert event.core_uuid == "core-a"


def test_empty_core_uuid_is_none() -> None:
    for blank in ("", "   "):
        event = parse_event(
            {"Event-Name": "CHANNEL_CREATE", "Core-UUID": blank, "Event-Sequence": "3"},
            received_at=1.0,
        )
        assert event.core_uuid is None
        # The sequence is still reported faithfully; pairing it with a core is
        # the consumer's decision.
        assert event.event_sequence == 3


def test_zero_event_sequence_is_a_real_value() -> None:
    event = parse_event(
        {"Event-Name": "CHANNEL_CREATE", "Core-UUID": "c", "Event-Sequence": "0"},
        received_at=1.0,
    )

    assert event.event_sequence == 0


def test_event_identity_survives_url_encoded_event_headers() -> None:
    from pyfreeswitch.clients.esl import ESLClient

    text = (
        "Event-Name: CHANNEL_CREATE\n"
        "Core-UUID: 6f1b2c3d%2D0000%2D4000%2D8000%2D000000000001\n"
        "Event-Sequence: %34%32\n"
        "Event-Date-Timestamp: 1764000000123456\n"
    )
    frame = ESLClient._parse_headers(text, decode_values=True)

    event = parse_event(frame, received_at=1.0)

    assert event.core_uuid == "6f1b2c3d-0000-4000-8000-000000000001"
    assert event.event_sequence == 42
    assert event.event_timestamp_us == 1764000000123456


def test_event_dto_constructors_default_identity_to_none() -> None:
    event = ChannelCreateEvent(event_name="CHANNEL_CREATE", received_at=0.0)

    assert event.core_uuid is None
    assert event.event_sequence is None
    assert event.event_timestamp_us is None

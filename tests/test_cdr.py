"""CDR row parsing against the 'unique' cdr_csv template."""

from __future__ import annotations

import pytest

from pyfreeswitch.models.cdr import CDRParseError, parse_cdr_row

# One answered intercom->queue leg in template column order (15 cols w/ recording).
_ANSWERED = [
    "1799",
    "Los Olmos",
    "99",
    "intercom",
    "2026-07-06 14:00:00",
    "2026-07-06 14:00:03",
    "2026-07-06 14:02:10",
    "130",
    "127",
    "NORMAL_CLEARING",
    "aaaa-uuid",
    "bbbb-uuid",
    "1799",
    "99",
    "aaaa-uuid.wav",
]

_UNANSWERED = [
    "1801",
    "",
    "99",
    "intercom",
    "2026-07-06 14:00:00",
    "",
    "2026-07-06 14:00:20",
    "20",
    "0",
    "NO_ANSWER",
    "cccc-uuid",
    "",
    "1801",
    "99",
    "",
]

# A legacy row written before the recording column existed (14 cols).
_LEGACY_14 = [
    "1500",
    "",
    "99",
    "intercom",
    "2026-07-06 14:00:00",
    "2026-07-06 14:00:02",
    "2026-07-06 14:01:00",
    "60",
    "58",
    "NORMAL_CLEARING",
    "dddd-uuid",
    "",
    "1500",
    "99",
]

_QUEUE_AGENT_LEG = [
    *_ANSWERED,
    "agent",
    "queue99@default",
    "101@default",
    "agent-leg-uuid",
    "member-uuid",
    "member-session-uuid",
    "1788955200",
    "1788955203",
    "1788955260",
    "0",
    "terminated",
    "",
]


def test_parse_answered_leg() -> None:
    rec = parse_cdr_row(_ANSWERED)
    assert rec.uuid == "aaaa-uuid"
    assert rec.bleg_uuid == "bbbb-uuid"
    assert rec.destination_number == "99"
    assert rec.duration == 130
    assert rec.billsec == 127
    assert rec.answered is True
    assert rec.answer_stamp is not None
    assert rec.start_stamp.year == 2026
    assert rec.recording == "aaaa-uuid.wav"


def test_legacy_14_column_row_parses_without_recording() -> None:
    rec = parse_cdr_row(_LEGACY_14)
    assert rec.uuid == "dddd-uuid"
    assert rec.billsec == 58
    assert rec.recording == ""


def test_parse_unanswered_leg_has_no_answer_stamp() -> None:
    rec = parse_cdr_row(_UNANSWERED)
    assert rec.uuid == "cccc-uuid"
    assert rec.bleg_uuid is None
    assert rec.answer_stamp is None
    assert rec.billsec == 0
    assert rec.answered is False


def test_parse_enriched_callcenter_leg() -> None:
    rec = parse_cdr_row(_QUEUE_AGENT_LEG)
    assert rec.callcenter_side == "agent"
    assert rec.queue_name == "queue99@default"
    assert rec.agent_name == "101@default"
    assert rec.agent_uuid == "agent-leg-uuid"
    assert rec.member_uuid == "member-uuid"
    assert rec.member_session_uuid == "member-session-uuid"
    assert rec.queue_joined_at is not None
    assert rec.queue_answered_at is not None
    assert rec.queue_terminated_at is not None
    assert rec.queue_canceled_at is None
    assert rec.queue_cause == "terminated"


def test_short_row_raises() -> None:
    with pytest.raises(CDRParseError):
        parse_cdr_row(["1799", "99"])


def test_missing_uuid_raises() -> None:
    row = list(_ANSWERED)
    row[10] = ""  # uuid column
    with pytest.raises(CDRParseError):
        parse_cdr_row(row)

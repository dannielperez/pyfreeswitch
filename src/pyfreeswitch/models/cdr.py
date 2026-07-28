"""Typed CDR record + parser for the UniqueOS ``cdr_csv`` "unique" template.

The FreeSWITCH core writes one CSV row per call leg to
``/var/log/freeswitch/cdr-csv/Master.csv`` using the ``unique`` template
(``autoload_configs/cdr_csv.conf.xml`` in unique-infra). This module owns the
column contract so the Django side never hard-codes field offsets — if the
template changes, it changes here and nowhere else.

Template (column order is the contract)::

    caller_id_number, caller_id_name, destination_number, context,
    start_stamp, answer_stamp, end_stamp, duration, billsec,
    hangup_cause, uuid, bleg_uuid, sip_from_user, sip_to_user, recording

The trailing ``recording`` column (the ``${recording_file}`` basename) is
**optional** — legacy 14-column rows written before recording was enabled still
parse. Only the first 14 columns are required.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

from pydantic import BaseModel
from pydantic import Field

from pyfreeswitch.exceptions import FreeSwitchError

# The ordered field names of the ``unique`` cdr_csv template. Keep in lockstep
# with unique-infra/core/config/freeswitch/autoload_configs/cdr_csv.conf.xml.
CDR_UNIQUE_COLUMNS: tuple[str, ...] = (
    "caller_id_number",
    "caller_id_name",
    "destination_number",
    "context",
    "start_stamp",
    "answer_stamp",
    "end_stamp",
    "duration",
    "billsec",
    "hangup_cause",
    "uuid",
    "bleg_uuid",
    "sip_from_user",
    "sip_to_user",
    "recording",
)

# The first N columns are mandatory; ``recording`` (the last) is optional so
# legacy rows written before recording was enabled still parse.
CDR_REQUIRED_COLUMNS = 14

# FreeSWITCH ${start_stamp} et al. render as "YYYY-MM-DD HH:MM:SS" (local to the
# box, which we run in UTC). Timestamps are optional (unanswered legs have an
# empty answer_stamp).
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


class CDRParseError(FreeSwitchError):
    """A CDR row did not match the expected ``unique`` template shape."""


class CallRecord(BaseModel):
    """One parsed CDR leg. ``uuid`` is the stable idempotency key."""

    uuid: str
    bleg_uuid: str | None = None
    caller_id_number: str = ""
    caller_id_name: str = ""
    destination_number: str = ""
    context: str = ""
    start_stamp: datetime | None = None
    answer_stamp: datetime | None = None
    end_stamp: datetime | None = None
    duration: int = 0
    billsec: int = 0
    hangup_cause: str = ""
    sip_from_user: str = ""
    sip_to_user: str = ""
    recording: str = ""
    raw: dict[str, str] = Field(default_factory=dict)

    @property
    def answered(self) -> bool:
        """True when the leg was answered (has an answer timestamp + billsec)."""
        return self.answer_stamp is not None and self.billsec > 0


def _parse_ts(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value or value in {"0", "1970-01-01 00:00:00"}:
        return None
    try:
        return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_int(value: str) -> int:
    try:
        return int((value or "0").strip() or 0)
    except ValueError:
        return 0


def parse_cdr_row(row: list[str]) -> CallRecord:
    """Parse one ``unique``-template CSV row into a :class:`CallRecord`.

    Args:
        row: The already-split CSV fields (quotes stripped by ``csv.reader``).

    Raises:
        CDRParseError: When the row has too few columns or an empty ``uuid``
            (without a stable key the consumer cannot upsert safely).
    """
    if len(row) < CDR_REQUIRED_COLUMNS:
        msg = (
            f"CDR row has {len(row)} columns, "
            f"expected >= {CDR_REQUIRED_COLUMNS} for the 'unique' template"
        )
        raise CDRParseError(msg)

    fields = dict(zip(CDR_UNIQUE_COLUMNS, row, strict=False))
    uuid = fields["uuid"].strip()
    if not uuid:
        msg = "CDR row is missing its uuid (no stable key to upsert on)"
        raise CDRParseError(msg)

    bleg = fields["bleg_uuid"].strip()
    return CallRecord(
        uuid=uuid,
        bleg_uuid=bleg or None,
        caller_id_number=fields["caller_id_number"].strip(),
        caller_id_name=fields["caller_id_name"].strip(),
        destination_number=fields["destination_number"].strip(),
        context=fields["context"].strip(),
        start_stamp=_parse_ts(fields["start_stamp"]),
        answer_stamp=_parse_ts(fields["answer_stamp"]),
        end_stamp=_parse_ts(fields["end_stamp"]),
        duration=_parse_int(fields["duration"]),
        billsec=_parse_int(fields["billsec"]),
        hangup_cause=fields["hangup_cause"].strip(),
        sip_from_user=fields["sip_from_user"].strip(),
        sip_to_user=fields["sip_to_user"].strip(),
        recording=fields.get("recording", "").strip(),
        raw=fields,
    )


__all__ = [
    "CDR_REQUIRED_COLUMNS",
    "CDR_UNIQUE_COLUMNS",
    "CDRParseError",
    "CallRecord",
    "parse_cdr_row",
]

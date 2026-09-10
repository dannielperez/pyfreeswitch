"""Typed, completeness-aware ``show channels as json`` results.

Source: FreeSWITCH mod_commands.c, show_function/show_as_json_callback.
Empty results may omit ``rows``. Row values are strings; row_count is a number.
Completeness describes the response, not an atomic view of call lifecycle.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from pyfreeswitch.config import MAX_ESL_MAX_BODY_BYTES

MAX_CHANNEL_ROWS = 10_000
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


class ChannelSnapshotState(str, Enum):
    COMPLETE = "complete"
    EMPTY = "empty"
    ERROR = "error"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class LiveChannel:
    uuid: str
    direction: str
    state: str
    call_uuid: str = ""
    name: str = ""
    caller_name: str = ""
    caller_number: str = ""
    destination: str = ""
    application: str = ""
    call_state: str = ""
    created_epoch: int | None = None


@dataclass(frozen=True, slots=True)
class ChannelSnapshot:
    state: ChannelSnapshotState
    channels: tuple[LiveChannel, ...] = ()
    row_count: int | None = None
    detail: str = ""
    source_host: str = ""
    source_port: int | None = None
    captured_at: datetime | None = None

    @property
    def is_authoritative(self) -> bool:
        """The response is complete; absence alone does not prove call ending."""
        return self.state in (ChannelSnapshotState.COMPLETE, ChannelSnapshotState.EMPTY)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-finite JSON number")


def _string(row, name, *, required=False):
    value = row.get(name, "")
    if not isinstance(value, str) or (required and not value):
        raise ValueError("invalid channel field")
    return value


def _channel(row) -> LiveChannel:
    if not isinstance(row, dict):
        raise ValueError("invalid channel row")
    uuid = _string(row, "uuid", required=True)
    call_uuid = _string(row, "call_uuid")
    direction = _string(row, "direction", required=True)
    if not _UUID.fullmatch(uuid) or (call_uuid and not _UUID.fullmatch(call_uuid)):
        raise ValueError("invalid channel identity")
    if direction not in {"inbound", "outbound"}:
        raise ValueError("invalid channel direction")
    epoch = _string(row, "created_epoch")
    if epoch and (not epoch.isascii() or not epoch.isdigit() or len(epoch) > 19):
        raise ValueError("invalid channel creation time")
    return LiveChannel(
        uuid=uuid.lower(),
        direction=direction,
        state=_string(row, "state", required=True),
        call_uuid=call_uuid.lower(),
        name=_string(row, "name"),
        caller_name=_string(row, "cid_name"),
        caller_number=_string(row, "cid_num"),
        destination=_string(row, "dest"),
        application=_string(row, "application"),
        call_state=_string(row, "callstate"),
        created_epoch=int(epoch) if epoch else None,
    )


def parse_channel_snapshot(text: str) -> ChannelSnapshot:
    """Parse a bounded response without returning partial rows as complete.

    Unknown fields are ignored. Invalid known fields, count mismatches and
    duplicates invalidate the whole result. Error details never echo payloads.
    Transport failures still raise from ESLClient before reaching this parser.
    """
    malformed = ChannelSnapshot(
        ChannelSnapshotState.MALFORMED, detail="invalid or incomplete channel snapshot"
    )
    if not isinstance(text, str) or len(text) > MAX_ESL_MAX_BODY_BYTES:
        return malformed
    try:
        if len(text.encode("utf-8")) > MAX_ESL_MAX_BODY_BYTES:
            return malformed
        if text.lstrip().lower().startswith(("-err", "-usage")):
            return ChannelSnapshot(
                ChannelSnapshotState.ERROR, detail="channel query refused"
            )
        value = json.loads(
            text, object_pairs_hook=_object, parse_constant=_reject_constant
        )
        if not isinstance(value, dict):
            return malformed
        count = value.get("row_count")
        if type(count) is not int or not 0 <= count <= MAX_CHANNEL_ROWS:
            return malformed
        rows = value.get("rows", [] if count == 0 else None)
        if not isinstance(rows, list) or len(rows) != count:
            return malformed
        channels = tuple(_channel(row) for row in rows)
        if len({channel.uuid for channel in channels}) != count:
            return malformed
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return malformed
    return ChannelSnapshot(
        ChannelSnapshotState.COMPLETE if count else ChannelSnapshotState.EMPTY,
        channels=channels,
        row_count=count,
    )


__all__ = [
    "ChannelSnapshot",
    "ChannelSnapshotState",
    "LiveChannel",
    "parse_channel_snapshot",
]

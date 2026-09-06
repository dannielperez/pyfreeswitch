"""Typed ``mod_callcenter`` runtime model: agent status/state, tiers, listings.

Vocabulary is FreeSWITCH's own (``mod_callcenter.c`` ``AGENT_STATUS_CHART`` /
``AGENT_STATE_CHART`` / tier ``STATE_CHART``). The literal strings matter: the
switch compares them case-insensitively but rejects anything not in the chart
(``-ERR Invalid Agent Status!``), so callers pass the enums, never free text.

Listing output (``callcenter_config agent list`` / ``tier list``) is produced by
``list_result_callback``: one ``|``-delimited header row built from the SQL
column names (``SELECT * FROM agents`` / ``tiers``), then one ``|``-delimited
row per record, then ``+OK``. Column *order* is therefore the switch's table
schema and may grow between versions; the parser keys by header name and never
by offset.

Semantics that differ from Asterisk queues (parity matrix PA rows): an
operator does not "add" themselves to a queue — they flip ``status`` on a
pre-provisioned agent that already has a tier for the queue. ``Logged Out``
is leave, ``Available`` is join, ``On Break`` is pause. Runtime status lives in
the callcenter DB but is re-applied from XML on ``reloadxml``/restart, so a
consumer must reconcile after those.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import Enum


class AgentStatus(str, Enum):
    """``agents.status`` — the operator's personal availability."""

    UNKNOWN = "Unknown"
    LOGGED_OUT = "Logged Out"
    AVAILABLE = "Available"
    AVAILABLE_ON_DEMAND = "Available (On Demand)"
    ON_BREAK = "On Break"

    @classmethod
    def from_text(cls, value: str) -> AgentStatus:
        lowered = (value or "").strip().lower()
        for member in cls:
            if member.value.lower() == lowered:
                return member
        return cls.UNKNOWN


class AgentState(str, Enum):
    """``agents.state`` — what the switch is doing with the agent right now."""

    UNKNOWN = "Unknown"
    WAITING = "Waiting"
    RECEIVING = "Receiving"
    IN_A_QUEUE_CALL = "In a queue call"
    IDLE = "Idle"

    @classmethod
    def from_text(cls, value: str) -> AgentState:
        lowered = (value or "").strip().lower()
        for member in cls:
            if member.value.lower() == lowered:
                return member
        return cls.UNKNOWN


class TierState(str, Enum):
    """``tiers.state`` — the agent's standing within one queue."""

    UNKNOWN = "Unknown"
    NO_ANSWER = "No Answer"
    READY = "Ready"
    OFFERING = "Offering"
    ACTIVE_INBOUND = "Active Inbound"
    STANDBY = "Standby"

    @classmethod
    def from_text(cls, value: str) -> TierState:
        lowered = (value or "").strip().lower()
        for member in cls:
            if member.value.lower() == lowered:
                return member
        return cls.UNKNOWN


#: Statuses in which the switch will offer queue calls to the agent.
ACTIVE_AGENT_STATUSES: frozenset[AgentStatus] = frozenset(
    {AgentStatus.AVAILABLE, AgentStatus.AVAILABLE_ON_DEMAND},
)


@dataclass(frozen=True, slots=True)
class CallCenterAgent:
    """One row of ``callcenter_config agent list``."""

    name: str
    status: AgentStatus = AgentStatus.UNKNOWN
    state: AgentState = AgentState.UNKNOWN
    contact: str = ""
    type: str = ""
    uuid: str = ""
    #: Every column the switch returned, keyed by header name (forward-compat).
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def logged_in(self) -> bool:
        return self.status in ACTIVE_AGENT_STATUSES

    @property
    def paused(self) -> bool:
        return self.status is AgentStatus.ON_BREAK


@dataclass(frozen=True, slots=True)
class CallCenterTier:
    """One row of ``callcenter_config tier list``."""

    queue: str
    agent: str
    state: TierState = TierState.UNKNOWN
    level: int = 1
    position: int = 1
    raw: dict[str, str] = field(default_factory=dict)


_ERROR_PREFIXES = ("-err", "-usage")
_OK_PREFIX = "+ok"


def parse_callcenter_table(text: str) -> list[dict[str, str]]:
    """Parse a ``|``-delimited ``list_result_callback`` table into row dicts.

    Returns ``[]`` for an empty listing (header only, or just ``+OK``). Raises
    :class:`ValueError` for an ``-ERR``/``-USAGE`` reply so an empty list is
    never mistaken for "no agents/tiers" after a refusal.
    """
    stripped = (text or "").strip()
    if not stripped:
        return []
    if stripped.lower().startswith(_ERROR_PREFIXES):
        msg = stripped.splitlines()[0][:200]
        raise ValueError(msg)

    lines = [
        line.strip()
        for line in stripped.splitlines()
        if line.strip() and not line.strip().lower().startswith(_OK_PREFIX)
    ]
    if not lines:
        return []
    header = [column.strip() for column in lines[0].split("|")]
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        values = line.split("|")
        # A short row is a truncated frame; skip rather than mis-key columns.
        if len(values) < len(header):
            continue
        rows.append(
            {
                name: value.strip()
                for name, value in zip(header, values, strict=False)
                if name
            },
        )
    return rows


def _int_or_default(value: str, default: int) -> int:
    try:
        return int((value or "").strip())
    except ValueError:
        return default


def parse_agent_list(text: str) -> list[CallCenterAgent]:
    """``callcenter_config agent list [name]`` → typed agents."""
    agents: list[CallCenterAgent] = []
    for row in parse_callcenter_table(text):
        name = row.get("name", "")
        if not name:
            continue
        agents.append(
            CallCenterAgent(
                name=name,
                status=AgentStatus.from_text(row.get("status", "")),
                state=AgentState.from_text(row.get("state", "")),
                contact=row.get("contact", ""),
                type=row.get("type", ""),
                uuid=row.get("uuid", ""),
                raw=row,
            ),
        )
    return agents


def parse_tier_list(text: str) -> list[CallCenterTier]:
    """``callcenter_config tier list`` → typed tiers (switch order: level, position)."""
    tiers: list[CallCenterTier] = []
    for row in parse_callcenter_table(text):
        queue = row.get("queue", "")
        agent = row.get("agent", "")
        if not queue or not agent:
            continue
        tiers.append(
            CallCenterTier(
                queue=queue,
                agent=agent,
                state=TierState.from_text(row.get("state", "")),
                level=_int_or_default(row.get("level", ""), 1),
                position=_int_or_default(row.get("position", ""), 1),
                raw=row,
            ),
        )
    return tiers


__all__ = [
    "ACTIVE_AGENT_STATUSES",
    "AgentState",
    "AgentStatus",
    "CallCenterAgent",
    "CallCenterTier",
    "TierState",
    "parse_agent_list",
    "parse_callcenter_table",
    "parse_tier_list",
]

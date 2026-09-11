# ruff: noqa: INP001, E501 - verbatim switch listing fixtures exceed the line cap
"""mod_callcenter typed listings, agent status/state writers and their gates."""

from __future__ import annotations

import pytest
from pyfreeswitch import ESLClient
from pyfreeswitch import ESLConfig
from pyfreeswitch import ESLError
from pyfreeswitch import AgentState
from pyfreeswitch import AgentStatus
from pyfreeswitch import NotSupportedError
from pyfreeswitch import TierState
from pyfreeswitch import parse_agent_list
from pyfreeswitch import parse_callcenter_table
from pyfreeswitch import parse_tier_list

_AUTH_VALUE = "test-" + "value"

# Exact shape of list_result_callback: header from SELECT * column names, one
# row per record, trailing +OK.
_AGENT_LIST = """name|system|uuid|type|contact|status|state|max_no_answer|wrap_up_time|reject_delay_time|busy_delay_time|no_answer_delay_time|last_bridge_start|last_bridge_end|last_offered_call|last_status_change|no_answer_count|calls_answered|talk_time|ready_time
1900@default|single_box||callback|[call_timeout=10]user/1900|Available|Waiting|3|10|10|10|10|0|0|0|1757160000|0|4|120|0
1901@default|single_box||callback|[call_timeout=10]user/1901|On Break|Idle|3|10|10|10|10|0|0|0|1757160000|0|0|0|0
1902@default|single_box||callback|[call_timeout=10]user/1902|Logged Out|Waiting|3|10|10|10|10|0|0|0|0|0|0|0|0
+OK
"""

_TIER_LIST = """queue|agent|state|level|position
soc@default|1900@default|Ready|1|1
soc@default|1901@default|Standby|1|2
noc@default|1900@default|Ready|2|1
+OK
"""


def _client(*, allow_mutations: bool = False) -> ESLClient:
    return ESLClient(
        ESLConfig(
            host="core.example",
            password=_AUTH_VALUE,
            allow_mutations=allow_mutations,
        ),
    )


# -- parsers -------------------------------------------------------------------


def test_parse_callcenter_table_keys_by_header_not_offset():
    rows = parse_callcenter_table("b|a\n2|1\n+OK\n")

    assert rows == [{"b": "2", "a": "1"}]


def test_parse_callcenter_table_empty_listing_is_empty_not_error():
    assert parse_callcenter_table("+OK\n") == []
    assert parse_callcenter_table("") == []
    assert parse_callcenter_table("queue|agent|state|level|position\n+OK\n") == []


def test_parse_callcenter_table_raises_on_refusal():
    with pytest.raises(ValueError, match="-ERR"):
        parse_callcenter_table("-ERR Invalid!\n")


def test_parse_callcenter_table_skips_truncated_rows():
    rows = parse_callcenter_table("a|b|c\n1|2|3\n4|5\n+OK\n")

    assert rows == [{"a": "1", "b": "2", "c": "3"}]


def test_parse_agent_list_types_status_and_state():
    agents = parse_agent_list(_AGENT_LIST)

    assert [agent.name for agent in agents] == [
        "1900@default",
        "1901@default",
        "1902@default",
    ]
    available, on_break, logged_out = agents
    assert available.status is AgentStatus.AVAILABLE
    assert available.state is AgentState.WAITING
    assert available.logged_in is True
    assert available.paused is False
    assert available.contact == "[call_timeout=10]user/1900"
    assert available.raw["calls_answered"] == "4"

    assert on_break.status is AgentStatus.ON_BREAK
    assert on_break.paused is True
    assert on_break.logged_in is False

    assert logged_out.status is AgentStatus.LOGGED_OUT
    assert logged_out.logged_in is False


def test_parse_tier_list_types_state_and_ints():
    tiers = parse_tier_list(_TIER_LIST)

    assert [(t.queue, t.agent) for t in tiers] == [
        ("soc@default", "1900@default"),
        ("soc@default", "1901@default"),
        ("noc@default", "1900@default"),
    ]
    assert tiers[0].state is TierState.READY
    assert tiers[1].state is TierState.STANDBY
    assert tiers[2].level == 2
    assert tiers[2].position == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Available", AgentStatus.AVAILABLE),
        ("available", AgentStatus.AVAILABLE),
        ("Available (On Demand)", AgentStatus.AVAILABLE_ON_DEMAND),
        ("On Break", AgentStatus.ON_BREAK),
        ("Logged Out", AgentStatus.LOGGED_OUT),
        ("Gone Fishing", AgentStatus.UNKNOWN),
        ("", AgentStatus.UNKNOWN),
    ],
)
def test_agent_status_from_text_matches_chart_case_insensitively(text, expected):
    assert AgentStatus.from_text(text) is expected


def test_agent_state_from_text_handles_multiword_chart_entry():
    assert AgentState.from_text("In a queue call") is AgentState.IN_A_QUEUE_CALL
    assert AgentState.from_text("in A QUEUE call") is AgentState.IN_A_QUEUE_CALL


# -- typed reads through the read-only allow-list ------------------------------


def test_typed_agent_list_uses_read_only_form(monkeypatch):
    client = _client()
    commands = []

    def fake_api(command, **_kw):
        commands.append(command)
        return _AGENT_LIST

    monkeypatch.setattr(client, "api", fake_api)

    agents = client.list_callcenter_agents_typed()

    assert commands == ["callcenter_config agent list"]
    assert len(agents) == 3


def test_typed_agent_list_for_one_agent_is_allow_listed():
    client = _client()
    monkeypatch_calls = []

    def fake_command(line):
        monkeypatch_calls.append(line)
        return {"_body": _AGENT_LIST}

    client._command = fake_command  # noqa: SLF001 - transport stub

    agents = client.list_callcenter_agents_typed("1900@default")

    assert monkeypatch_calls == ["api callcenter_config agent list 1900@default"]
    assert agents[0].name == "1900@default"


def test_typed_agent_list_rejects_injection_in_agent_name():
    client = _client()

    with pytest.raises(NotSupportedError):
        client.list_callcenter_agents_typed("1900 ; uuid_kill x")


def test_typed_agent_list_raises_on_refusal(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "api", lambda *_a, **_k: "-ERR Invalid!\n")

    with pytest.raises(ESLError):
        client.list_callcenter_agents_typed()


def test_typed_tier_list(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "api", lambda *_a, **_k: _TIER_LIST)

    tiers = client.list_callcenter_tiers_typed()

    assert len(tiers) == 3


def test_get_agent_status_parses_bare_value():
    client = _client()
    lines = []

    def fake_command(line):
        lines.append(line)
        return {"_body": "Available\n"}

    client._command = fake_command  # noqa: SLF001

    assert client.get_callcenter_agent_status("1900@default") is AgentStatus.AVAILABLE
    assert lines == ["api callcenter_config agent get status 1900@default"]


def test_get_agent_state_parses_bare_value(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "api", lambda *_a, **_k: "In a queue call\n")

    assert (
        client.get_callcenter_agent_state("1900@default") is AgentState.IN_A_QUEUE_CALL
    )


def test_get_agent_status_raises_on_not_found(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "api", lambda *_a, **_k: "-ERR Agent not found!\n")

    with pytest.raises(ESLError, match="Agent not found"):
        client.get_callcenter_agent_status("1999@default")


# -- typed writers -------------------------------------------------------------


def test_set_agent_status_is_gated_by_allow_mutations():
    client = _client(allow_mutations=False)
    sent = []
    client._command = lambda line: sent.append(line) or {"_body": "+OK\n"}  # noqa: SLF001

    with pytest.raises(NotSupportedError, match="read-only"):
        client.set_callcenter_agent_status("1900@default", AgentStatus.AVAILABLE)

    assert sent == []


def test_set_agent_status_builds_quoted_exact_command():
    client = _client(allow_mutations=True)
    sent = []
    client._command = lambda line: sent.append(line) or {"_body": "+OK\n"}  # noqa: SLF001

    reply = client.set_callcenter_agent_status(
        "1900@default",
        AgentStatus.AVAILABLE_ON_DEMAND,
    )

    assert reply.ok is True
    assert sent == [
        "api callcenter_config agent set status 1900@default 'Available (On Demand)'",
    ]


def test_set_agent_status_reports_agent_not_found():
    client = _client(allow_mutations=True)
    client._command = lambda _line: {"_body": "-ERR Agent not found!\n"}  # noqa: SLF001

    reply = client.set_callcenter_agent_status("1999@default", AgentStatus.LOGGED_OUT)

    assert reply.ok is False
    assert reply.not_found is True


def test_set_agent_status_rejects_unknown_and_free_text():
    client = _client(allow_mutations=True)

    with pytest.raises(NotSupportedError):
        client.set_callcenter_agent_status("1900@default", AgentStatus.UNKNOWN)
    with pytest.raises(NotSupportedError):
        client.set_callcenter_agent_status("1900@default", "Available")  # type: ignore[arg-type]


def test_set_agent_status_rejects_injection_in_agent_name():
    client = _client(allow_mutations=True)
    sent = []
    client._command = lambda line: sent.append(line) or {"_body": "+OK\n"}  # noqa: SLF001

    with pytest.raises(NotSupportedError):
        client.set_callcenter_agent_status(
            "1900@default 'x' ; hupall", AgentStatus.AVAILABLE
        )

    assert sent == []


def test_set_agent_state_builds_quoted_exact_command():
    client = _client(allow_mutations=True)
    sent = []
    client._command = lambda line: sent.append(line) or {"_body": "+OK\n"}  # noqa: SLF001

    reply = client.set_callcenter_agent_state("1900@default", AgentState.WAITING)

    assert reply.ok is True
    assert sent == ["api callcenter_config agent set state 1900@default 'Waiting'"]


def test_callcenter_cleanup_uses_canonical_names_and_is_idempotent():
    client = _client(allow_mutations=True)
    sent = []
    client._command = lambda line: sent.append(line) or {"_body": "+OK\n"}  # noqa: SLF001

    for _ in range(2):
        tier_reply = client.delete_callcenter_tier(
            "acceptance@default",
            "990001@default",
        )
        agent_reply = client.delete_callcenter_agent("990001@default")

        assert tier_reply.ok is True
        assert agent_reply.ok is True

    assert sent == [
        "api callcenter_config tier del acceptance@default 990001@default",
        "api callcenter_config agent del 990001@default",
        "api callcenter_config tier del acceptance@default 990001@default",
        "api callcenter_config agent del 990001@default",
    ]


def test_callcenter_cleanup_rejects_short_names_before_sending():
    client = _client(allow_mutations=True)
    sent = []
    client._command = lambda line: sent.append(line) or {"_body": "+OK\n"}  # noqa: SLF001

    with pytest.raises(NotSupportedError, match="unqualified callcenter queue"):
        client.delete_callcenter_tier("acceptance", "990001@default")
    with pytest.raises(NotSupportedError, match="unqualified callcenter agent"):
        client.delete_callcenter_tier("acceptance@default", "990001")
    with pytest.raises(NotSupportedError, match="unqualified callcenter agent"):
        client.delete_callcenter_agent("990001")
    with pytest.raises(NotSupportedError, match="unqualified callcenter agent"):
        client.delete_callcenter_agent("990001@default@extra")

    assert sent == []


def test_callcenter_cleanup_is_gated_by_allow_mutations():
    client = _client(allow_mutations=False)
    sent = []
    client._command = lambda line: sent.append(line) or {"_body": "+OK\n"}  # noqa: SLF001

    with pytest.raises(NotSupportedError, match="read-only"):
        client.delete_callcenter_tier("acceptance@default", "990001@default")
    with pytest.raises(NotSupportedError, match="read-only"):
        client.delete_callcenter_agent("990001@default")

    assert sent == []


def test_empty_reply_to_a_mutation_is_not_success():
    """A dropped read must never be reported as 'the switch acted'."""
    client = _client(allow_mutations=True)
    client._command = lambda _line: {}  # noqa: SLF001

    reply = client.set_callcenter_agent_status("1900@default", AgentStatus.AVAILABLE)

    assert reply.ok is False
    assert "empty" in reply.detail

# pyfreeswitch

Typed Python library for **FreeSWITCH** — the SDK boundary for the UniqueOS
telephony pipeline. It owns *all* FreeSWITCH protocol, transport, and parsing;
the Django app (`uniqueos.telephony`) does persistence, orchestration, and UI on
top of the typed objects this library returns. (Same boundary rule the codebase
enforces for `pytvt`, `pyakuvox`, `pyfreepbx` — CLAUDE.md §4.)

## What it does

- **Event Socket Layer (ESL) inbound client** — connect, authenticate, subscribe
  to `event plain`, and stream **typed events** (`CHANNEL_CREATE`,
  `CHANNEL_ANSWER`, `CHANNEL_BRIDGE`, `CHANNEL_HANGUP_COMPLETE`, and
  `mod_callcenter` `callcenter::info` ACD events).
- **`ESLEventListener`** — a synchronous, reconnecting event stream (the shape a
  long-lived UniqueOS runner supervises), with an `ESL_IDLE` liveness tick.
- **CDR parser** — turns the core's `cdr_csv` "unique" template rows into typed
  `CallRecord` objects. The column contract lives here, not in the app.

## Safe by default

### Command replies and stream recovery

The inbound client follows the event/reply separation used by
[upstream libesl](https://github.com/signalwire/freeswitch/blob/master/libs/esl/src/esl.c).
Events arriving during a command are retained in order for `read_event()`;
buffering is capped at 64 events and the configured `max_frame_bytes` byte budget.
Overflow closes the connection with `ESLProtocolError`. All reply reads share one
command deadline, so event traffic cannot extend a blocked command indefinitely.
Use one caller per client and separate connections for concurrent consumers.

An empty read timeout is an idle tick. An incomplete-frame timeout is an
`ESLConnectionError`, causing the listener to reconnect. Command timeouts close
the connection and never replay the command; a mutation may already have run,
so reconcile its result before deciding whether to retry.

This is remote `mod_event_socket` control; it does not require the server-side
`mod_python3` interpreter or Sangoma's native SWIG package.

### Command gates

ESL can control the switch (`originate`, `uuid_kill`, `reloadxml`, `hupall` …).
This client is **read-only by default**: `ESLClient.api()` only runs an
allow-listed set of status verbs (`status`, `sofia`, `show`,
`callcenter_config … list|get` …). Raw mutating strings require an explicit
`allow_unsafe=True` opt-in. The listener path issues no commands beyond its
event subscription. The event-socket password is passed in via `ESLConfig` and
never logged.

## Typed mutating commands (opt-in)

The operator-console capabilities UniqueOS needs are exposed as **typed
writers** that validate every token, build the one exact command form from
`mod_callcenter.c` / `mod_commands.c`, and return a `CommandReply` (`ok`,
`detail`, `not_found`). They are gated by `ESLConfig(allow_mutations=True)` —
a listener or a read-only probe cannot reach them, and `allow_mutations` never
unlocks raw `api()` strings.

| Capability | Method | Command |
|---|---|---|
| queue join / leave / pause | `set_callcenter_agent_status(agent, AgentStatus)` | `callcenter_config agent set status <agent> '<status>'` |
| reconcile stuck agent | `set_callcenter_agent_state(agent, AgentState)` | `callcenter_config agent set state <agent> '<state>'` |
| membership / reconcile (read) | `list_callcenter_agents_typed()`, `list_callcenter_tiers_typed()`, `get_callcenter_agent_status()` | `callcenter_config agent list [agent]`, `tier list`, `agent get status` |
| hang up | `uuid_kill(uuid, cause=None)` | `uuid_kill <uuid> [cause]` |
| blind transfer | `uuid_transfer(uuid, dest, leg=, dialplan=, context=)` | `uuid_transfer <uuid> [-bleg\|-both] <dest> [<dialplan>] [<context>]` |
| warm transfer (start) | `uuid_attended_transfer(uuid, dialstring)` | `uuid_broadcast <uuid> att_xfer::<dialstring> aleg` |
| recording | `uuid_record(uuid, "start"\|"stop", path)` | `uuid_record <uuid> start\|stop <path>` |

mod_callcenter semantics differ from Asterisk queues: an operator does not
"add" themselves to a queue — they flip `status` on a pre-provisioned agent
that already has a tier for the queue (`Available` = join, `Logged Out` =
leave, `On Break` = pause). Runtime status is re-applied from XML on
`reloadxml`/restart, so consumers reconcile with `list_callcenter_agents_typed()`.
`att_xfer` has no "complete" command: the transfer completes when the
transferrer hangs up.

```python
from pyfreeswitch import ESLConfig, ESLEventListener, ESL_IDLE

cfg = ESLConfig(host="10.254.250.12", port=8021, password="…")
listener = ESLEventListener(cfg)
listener.start()
for event in listener.listen():
    if event is ESL_IDLE:
        continue
    print(event.event_name, event.call_uuid)
```

```python
import csv
from pyfreeswitch import parse_cdr_row

with open("Master.csv", newline="") as fh:
    for row in csv.reader(fh):
        record = parse_cdr_row(row)   # -> CallRecord(uuid=…, billsec=…, answered=…)
```

## Layout

```
src/pyfreeswitch/
  config.py              ESLConfig (pydantic-settings, env_prefix ESL_)
  exceptions.py          FreeSwitchError / ESLError / ESLAuthError / ESLTimeout …
  clients/esl.py         ESLClient — framed socket transport + read-only api()
  clients/esl_listener.py ESLEventListener — reconnecting typed event stream
  clients/esl_parser.py  parse_event() — frame -> typed DTO (pure)
  models/events.py       ESLEvent DTOs
  models/cdr.py          CallRecord + parse_cdr_row()
  models/callcenter.py   AgentStatus/AgentState/TierState, agent + tier list parsers
  models/commands.py     CommandReply + parse_command_reply() (+OK / -ERR)
```

Consumed by UniqueOS as an editable path dependency (`vendor/pyfreeswitch`);
destined to become the `dannielperez/pyfreeswitch` submodule alongside the other
`py*` vendor libs.

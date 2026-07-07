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

ESL can control the switch (`originate`, `uuid_kill`, `reloadxml`, `hupall` …).
This client is **read-only by default**: `ESLClient.api()` only runs an
allow-listed set of status verbs (`status`, `sofia`, `show`,
`callcenter_config` …). Mutating commands require an explicit `allow_unsafe=True`
opt-in. The listener path issues no commands beyond its event subscription. The
event-socket password is passed in via `ESLConfig` and never logged.

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
```

Consumed by UniqueOS as an editable path dependency (`vendor/pyfreeswitch`);
destined to become the `dannielperez/pyfreeswitch` submodule alongside the other
`py*` vendor libs.

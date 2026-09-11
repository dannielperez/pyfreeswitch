# Callcenter cleanup idempotency handoff

## Changed files and why

- `src/pyfreeswitch/clients/esl.py`: adds typed, mutation-gated tier and agent
  deletion methods. Cleanup requires the canonical `name@domain` identifiers
  returned by FreeSWITCH so a short name cannot receive `+OK` while leaving the
  domain-qualified database row behind.
- `tests/test_callcenter.py`: covers exact commands, repeated cleanup, mutation
  gating, and rejection of short identifiers before transport I/O.
- `README.md`: documents the supported cleanup surface.

## Validation

- `.venv/bin/python -m pytest tests/test_callcenter.py -q` — 32 passed.
- `.venv/bin/python -m pytest -q` — 237 passed.
- `.venv/bin/ruff check .` — passed.
- `.venv/bin/ruff format --check src/ tests/` — passed.
- `git diff --check` — passed.

## Risk and compatibility

The change is additive. Existing reads and agent status/state writers are
unchanged. Deletion remains disabled unless `ESLConfig.allow_mutations=True`.
Callers must pass the exact domain-qualified names returned by the typed list
methods.

## Blockers and next step

After merge, update the UniqueOS vendor pin through its normal reviewed PR and
use these typed methods in future synthetic acceptance cleanup. No deployment is
part of this branch.

# FruitcakeAI v0.6.2 (Draft)

## Summary
- Upgraded backend runtime baseline to Python 3.11.
- Rebuilt dependency set on Python 3.11 and applied security upgrades previously blocked on Python 3.9.
- Preserved task/webhook and Task 48 magazine behavior (no API contract changes).

## Runtime
- New required runtime: Python 3.11+.
- Added `.python-version` for local environment consistency.
- Updated startup behavior to create `.venv` with `python3.11`.

## Dependency Security Updates
- `python-multipart` -> `0.0.22`
- `nltk` -> `3.9.3`
- `pillow` -> `12.1.1`
- `filelock` -> `3.20.3`

## Validation
- `pip check`: no broken requirements.
- `pip-audit -r requirements.txt`: 1 advisory remains.
- `pytest -q`: 139 passed on Python 3.11.
- `pytest -q tests/test_auth.py tests/test_webhooks.py`: 26 passed.

## Residual Advisory
- `ecdsa 0.19.1` — `CVE-2024-23342` (no fixed version currently available in advisory output).

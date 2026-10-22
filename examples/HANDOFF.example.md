# Handoff: billing-api

_Saved 2026-10-22T18:42:07+05:30, at 104,213 tokens of context (≈52% of a 200k window)._

## Facts (recorded by ContextRail)
- Branch `feature/rate-limit` at `3f9c2ab71` Add token bucket limiter
- Uncommitted (2): `api/app.py`, `tests/test_app.py`
- Edited this session: `api/limits.py`, `tests/test_limits.py`, `api/app.py`
- Recent checks: `pytest -q tests/test_limits.py` passed; `pytest -q` failed

<!-- contextrail:narrative -->

## Goal
Add rate limiting to the public API so one client cannot starve the rest.

## Done
- Token bucket in `api/limits.py`, unit tests pass (`pytest tests/test_limits.py`).

## In progress
- Wiring the limiter into `api/app.py:42` (middleware registered, config not read yet).

## Next steps
1. Read `RATE_LIMIT_PER_MIN` from settings in `api/app.py`.
2. Add an integration test for the 429 response.

## Decisions
- Per-API-key buckets, not per-IP: the user's clients sit behind one NAT.

## Dead ends
- Redis-backed counter: too slow in tests; in-memory is enough for one instance.

## Open questions
- None

## Verify first
- `pytest -q`

## Resume prompt
Continue the API rate limiting: read RATE_LIMIT_PER_MIN in api/app.py, then add the 429 integration test.

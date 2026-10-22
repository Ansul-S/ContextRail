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

## User's rules
- "Don't add new dependencies without asking me."
- "Ask me before you change anything clients can see in a response."
- "Keep the limit configurable, ops tune it per environment."

## Done
- `TokenBucket` in `api/limits.py`; `pytest -q tests/test_limits.py` passed (12 tests).

## In progress
- Wiring the limiter into `create_app()` in `api/app.py`: middleware registered, `RATE_LIMIT_PER_MIN` not read yet. Unverified: no test covers it.

## Next steps
1. Latest request, "wire the limiter in and make the limit configurable": read `RATE_LIMIT_PER_MIN` from `Settings` in `api/settings.py` and pass it to `TokenBucket` in `create_app()`.
2. Register the limiter after the auth middleware, so `test_requires_key` passes again.
3. Add an integration test for the 429 response in `tests/test_app.py`.

## Decisions
- Per-API-key buckets, not per-IP: the user's clients sit behind one NAT.

## Dead ends
- Redis-backed counter: too slow in tests; in-memory is enough for one instance.

## Known facts
- 3 routes need the limiter: `/v1/invoices`, `/v1/customers`, `/v1/payments`. `/healthz` stays unlimited.
- The spec's default is 600 requests per minute per key (ticket BILL-482).
- `pytest -q` fails only in `test_requires_key`: the limiter runs before auth, so a request without a key gets 429 instead of 401. Unconfirmed whether `/healthz` is affected too.

## How to run
- Tests: `pytest -q` from the repository root with `BILLING_ENV=test`; the default `dev` needs a running database.
- The pre-commit hook runs `ruff check`. Fix what it reports; don't use `--no-verify`.

## Open questions
- Should a 429 carry a `Retry-After` header? The spec doesn't say, and clients retry at once today. (a) Add it: clients can back off, but the response changes, which needs the user's approval. (b) Leave it out: nothing changes for clients. Recommendation: (a).

## Verify first
- `pytest -q` (expect only `test_requires_key` to fail)

## Resume prompt
Continue the API rate limiting from the handoff: read RATE_LIMIT_PER_MIN into create_app(), fix the limiter-before-auth order, then add the 429 test. Ask me about Retry-After first.

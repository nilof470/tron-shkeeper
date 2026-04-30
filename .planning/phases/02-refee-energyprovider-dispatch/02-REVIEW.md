# Phase 2 Code Review

**Date:** 2026-04-30
**Scope:** `app/refee.py`, `app/config.py`, `app/energy_provider.py`, `app/tasks.py`

## Findings

### HIGH - `completed` was treated as safe before broadcasting

`app/energy_provider.py` accepted both `delegated` and `completed` as successful
polling results. `REQ-002` says the sweep should wait for `status="delegated"`
before broadcasting. In the re:Fee lifecycle, `completed` can mean the rental is
already returned, so broadcasting after `completed` could fail with insufficient
energy or burn TRX unexpectedly.

**Fix applied:** `SUCCESS_STATUSES` is now only `{"delegated"}` and `completed`
is treated as terminal failure for the pre-broadcast acquire path.

**Verification:** `/tmp/refee_completed_status_smoke.py` prints
`completed status rejected OK`.

### MEDIUM - malformed API JSON or fullnode resource check could bypass fallback

`_create_order()` and `_wait_until_delegated()` parsed JSON but did not verify the
payload was an object before later `.get(...)` calls. The final on-chain
`get_account_resource(receiver)` check could also raise and escape `acquire()`,
preventing the caller from applying the configured fallback.

**Fix applied:** both create/poll responses now require `dict`, and on-chain
resource check exceptions are caught and converted to `False`.

**Verification:** provider happy-path smoke and import sanity still pass.

### MEDIUM - non-positive REFEE timing/factor config was accepted

`poll_interval_sec`, `timeout_sec`, and `energy_overprovision_factor` accepted
zero or negative values. That could cause tight loops, immediate timeouts, or
invalid order amounts.

**Fix applied:** `RefeeConfig` now uses `Field(..., gt=0)` for these numeric
fields and `min_length=1` for `api_base_url`.

**Verification:** `REFEE='{"api_key":"secret","poll_interval_sec":0}'` fails at
startup with a Pydantic `greater_than` validation error.

## Residual Risk

Live re:Fee latency, rate-limit headers, and refund/error-body behavior remain
unverified until companion spike 003 is run with operator credentials and balance.

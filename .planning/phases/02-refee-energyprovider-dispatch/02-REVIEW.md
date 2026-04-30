---
status: issues_found
phase: 02-refee-energyprovider-dispatch
depth: deep
files_reviewed: 4
files:
  - app/refee.py
  - app/config.py
  - app/energy_provider.py
  - app/tasks.py
findings:
  critical: 0
  warning: 1
  info: 2
  total: 3
reviewed_at: 2026-04-30
---

# Phase 2 Deep Code Review

## Open Findings

### WR-001 - WARNING - bandwidth checks still bypass the sweep-selected TRON client

**Location:** `app/tasks.py:189`, `app/tasks.py:224`, `app/tasks.py:358` call
`has_free_bw(...)`; `app/utils.py:160` obtains a fresh
`ConnectionManager.client()`.

Phase 2 correctly passes the selected `tron_client` into
`get_energy_provider(tron_client=tron_client)`, so re:Fee post-delegation energy
verification uses the same fullnode chosen at the start of the sweep. However,
the surrounding bandwidth checks still call `has_free_bw(...)`, and that helper
creates a new client through `ConnectionManager.client()`.

In multi-server mode, `current_server_id` can change during a sweep. That means a
single re:Fee sweep can:

1. read account state / estimate energy on fullnode A,
2. rent energy and verify `EnergyLimit` on fullnode A,
3. check bandwidth on fullnode B,
4. then broadcast with the original `tron_client` from fullnode A.

If fullnode B lags or has a different resource view, the sweep can terminate
after a successful re:Fee rental, or proceed based on a bandwidth result that is
not from the broadcast client. This is the same class of consistency issue fixed
earlier for `StakingEnergyProvider`.

**Recommendation:** make `has_free_bw(account, tx_bw, use_only_staked=False,
tron_client=None)` use the provided client when present, defaulting to
`ConnectionManager.client()` for existing callers. Then pass the sweep-local
`tron_client` from all `transfer_trc20_from` calls in Phase 2 paths.

**Why not critical:** this does not expose funds directly and the existing
fallback paths still prevent blind token broadcast on failed energy acquisition.
The risk is inconsistent sweep decisions in multi-server deployments.

### IN-001 - INFO - empty re:Fee API key is accepted as configured

**Location:** `app/refee.py:9`

`api_key` is a `SecretStr`, but it has no minimum length constraint. As a result,
`REFEE='{"api_key":""}'` can pass startup validation and fail only at runtime
with rejected API requests.

**Recommendation:** use `api_key: SecretStr = Field(..., min_length=1)` if
Pydantic accepts the constraint for `SecretStr` in this version; otherwise add a
small field validator. This is a fail-fast configuration improvement.

### IN-002 - INFO - `SUCCESS_STATUSES` is unused

**Location:** `app/energy_provider.py:165`, `app/energy_provider.py:290`

`SUCCESS_STATUSES = {"delegated"}` documents the intended success set, but the
poll loop checks `if status == "delegated"` directly. This is not a behavior bug,
but it makes future status changes easier to apply incorrectly.

**Recommendation:** replace the direct equality check with
`if status in self.SUCCESS_STATUSES:`.

## Previously Fixed Findings

### Fixed - HIGH - `completed` was treated as safe before broadcasting

`app/energy_provider.py` originally accepted both `delegated` and `completed` as
successful polling results. `REQ-002` says the sweep should wait for
`status="delegated"` before broadcasting. In the re:Fee lifecycle, `completed`
can mean the rental is already returned, so broadcasting after `completed` could
fail with insufficient energy or burn TRX unexpectedly.

**Fix applied:** `completed` is now treated as terminal failure for the
pre-broadcast acquire path.

**Verification:** `/tmp/refee_completed_status_smoke.py` prints
`completed status rejected OK`.

### Fixed - MEDIUM - malformed API JSON or fullnode resource check could bypass fallback

`_create_order()` and `_wait_until_delegated()` parsed JSON but did not verify the
payload was an object before later `.get(...)` calls. The final on-chain
`get_account_resource(receiver)` check could also raise and escape `acquire()`,
preventing the caller from applying the configured fallback.

**Fix applied:** both create/poll responses now require `dict`, and on-chain
resource check exceptions are caught and converted to `False`.

### Fixed - MEDIUM - non-positive REFEE timing/factor config was accepted

`poll_interval_sec`, `timeout_sec`, and `energy_overprovision_factor` accepted
zero or negative values. That could cause tight loops, immediate timeouts, or
invalid order amounts.

**Fix applied:** `RefeeConfig` now uses `Field(..., gt=0)` for these numeric
fields and `min_length=1` for `api_base_url`.

## Positive Checks

- `RefeeEnergyProvider.acquire()` reads `config.REFEE` lazily and returns
  `False`, not an exception, for missing settings, rejected create-order calls,
  malformed JSON, terminal order statuses, polling errors, timeout, and failed
  on-chain verification.
- The re:Fee API key is passed only through request headers and is not logged.
- `get_energy_provider(tron_client=tron_client)` preserves the selected
  fullnode for provider operations.
- Default behavior remains `ENERGY_SOURCE=staking`, so deployments that do not
  opt into re:Fee keep the Phase 1 staking path.
- `transfer_trc20_from` preserves staking acquire-failure behavior and gates
  re:Fee burn fallback behind the existing
  `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT` flag.

## Residual Risk

Live re:Fee latency, rate-limit headers, status casing, and refund/error-body
behavior remain unverified until companion spike 003 is run with operator
credentials and a funded re:Fee balance.

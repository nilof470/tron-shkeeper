---
status: all_fixed
phase: 02-refee-energyprovider-dispatch
review: 02-REVIEW.md
findings_in_scope: 3
fixed: 3
skipped: 0
iteration: 1
fixed_at: 2026-04-30
---

# Phase 2 Code Review Fix Summary

## Fixed Findings

### WR-001 - bandwidth checks bypass the sweep-selected TRON client

**Files changed:**
- `app/utils.py`
- `app/tasks.py`

`has_free_bw(...)` now accepts an optional `tron_client` and uses it when
provided. `transfer_trc20_from` passes the sweep-local `tron_client` into its
three bandwidth checks, so the re:Fee/staking sweep no longer switches fullnode
for those resource checks in multi-server mode.

### IN-001 - empty re:Fee API key accepted as configured

**Files changed:**
- `app/refee.py`

`RefeeConfig` now validates `api_key` with a field validator and rejects empty
secrets at startup.

### IN-002 - `SUCCESS_STATUSES` unused

**Files changed:**
- `app/energy_provider.py`

The re:Fee poll loop now checks `status in self.SUCCESS_STATUSES`, keeping the
declared status set and the implementation aligned.

## Regression Coverage

**Files added:**
- `tests/test_phase2_review_fixes.py`

The regression tests cover:
- `has_free_bw(..., tron_client=...)` uses the provided client.
- `transfer_trc20_from` passes its selected `tron_client` to all local
  bandwidth checks.
- empty `RefeeConfig.api_key` raises `ValidationError`.
- `_wait_until_delegated` uses `SUCCESS_STATUSES` rather than a hard-coded
  success string.

## Verification

- `python -m unittest tests.test_phase2_review_fixes` passed.
- `python -m unittest discover -s tests` passed.
- `compileall app/refee.py app/config.py app/energy_provider.py app/tasks.py app/utils.py tests/test_phase2_review_fixes.py` passed.
- Default staking config smoke printed `default staking OK`.
- Valid re:Fee config smoke printed `refee config OK SecretStr`.
- Empty re:Fee API key now fails startup with `api_key must not be empty`.
- Invalid `REFEE.poll_interval_sec=0` still fails startup with `greater_than`.
- Mocked re:Fee provider happy path printed `provider happy path OK`.
- Mocked `completed` status rejection printed `completed status rejected OK`.
- Mocked re:Fee failure plus TRX-burn fallback printed `fallback path OK`.
- Import sanity printed `import sanity OK`.

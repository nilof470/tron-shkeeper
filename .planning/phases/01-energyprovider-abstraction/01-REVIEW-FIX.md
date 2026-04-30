---
phase: 01-energyprovider-abstraction
reviewed: 2026-04-30T13:04:10Z
fixed_at: 2026-04-30T13:13:42Z
fix_scope: all
findings_in_scope: 2
fixed: 2
skipped: 0
iteration: 1
status: all_fixed
fix_commits:
  - f68e27f
  - 4902690
---

# Phase 1 Code Review Fix Report

## Summary

Resolved both findings from `01-REVIEW.md`.

## Fixes Applied

### WR-01: Staking provider no longer uses the transfer's selected TRON client

**Status:** fixed  
**Commit:** `f68e27f` (`fix(01): reuse selected tron client in staking provider`)

`transfer_trc20_from()` now passes its already-selected `tron_client` to `get_energy_provider(tron_client=tron_client)`. `StakingEnergyProvider` stores that client and uses it inside `acquire()`. Direct provider use still has a fallback to `ConnectionManager.client()` when no client is injected.

This restores the pre-refactor behavior where the staking delegation closure used the same selected TRON client as the surrounding sweep.

### IN-01: `json` and `math` imports are now unused in `app/tasks.py`

**Status:** fixed  
**Commit:** `4902690` (`chore(01): remove stale tasks imports`)

Removed the stale `json` and `math` imports from `app/tasks.py`.

## Verification

- RED regression before fix: local stub failed with `AssertionError: second ConnectionManager.client() call`.
- GREEN regression after fix: same local stub passed with `connection_manager_client_calls=1`.
- Mode B smoke passed: mocked delegation, TRC-20 transfer, and `undelegate_energy.delay` all completed using the selected client.
- `PYTHONPYCACHEPREFIX=/tmp/tron-shkeeper-pycache /tmp/tron-shkeeper-py312-venv/bin/python -m compileall app/tasks.py app/energy_provider.py`
- Python 3.12 import sanity passed for `app.energy_provider` and `app.tasks`.
- AST checks confirmed no nested functions remain in `transfer_trc20_from`, `json`/`math` are not referenced in `app/tasks.py`, and provider wiring is `get_energy_provider(tron_client=tron_client)`.
- `git diff master...HEAD -- app/config.py requirements.txt | wc -l` returned `0`.

## Remaining Risk

No known review findings remain. The phase still awaits the existing human verification gate in `01-03-SMOKE.md`.

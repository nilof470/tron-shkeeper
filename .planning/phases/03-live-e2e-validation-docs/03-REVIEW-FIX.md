---
status: all_fixed
phase: 03-live-e2e-validation-docs
review: 03-REVIEW.md
findings_in_scope: 3
fixed: 3
skipped: 0
fixed_at: 2026-04-30
---

# Phase 3 Subagent Review Fix Summary

## Fixed Findings

### Used energy now reduces available energy

**Files changed:**
- `app/utils.py`
- `app/tasks.py`
- `app/energy_provider.py`

Added `get_available_energy(account_resource)` and replaced direct
`EnergyLimit` checks in sweep/provider energy decisions with
`max(EnergyLimit - EnergyUsed, 0)`.

### re:Fee no longer treats bandwidth delegation as energy delegation

**Files changed:**
- `app/tasks.py`

When `ENERGY_SOURCE=refee`, the sweep now rents the missing energy delta based
on available energy. The broad `fromAccounts` gate remains only in the legacy
staking provider path.

### re:Fee order amount uses the missing energy delta

**Files changed:**
- `app/energy_provider.py`

`RefeeEnergyProvider.acquire(...)` now sizes the paid re:Fee order from
`energy_to_provision * energy_overprovision_factor`, while still verifying
post-rental available energy against `minimum_energy_required`.

## Regression Coverage

**Files added:**
- `tests/test_refee_energy_accounting.py`

The regression tests cover:
- partially used delegated energy triggers a re:Fee top-up for the missing delta;
- existing delegated bandwidth does not block re:Fee energy rental;
- re:Fee orders use the missing delta but verify the full required available energy.

## Verification

- `/tmp/tron-shkeeper-py312-venv/bin/python -m unittest tests.test_refee_energy_accounting` passed.
- `/tmp/tron-shkeeper-py312-venv/bin/python -m unittest discover -s tests` passed.
- `/tmp/tron-shkeeper-py312-venv/bin/python -m py_compile app/refee.py app/config.py app/energy_provider.py app/tasks.py app/utils.py tests/test_phase2_review_fixes.py tests/test_refee_bandwidth_guard.py tests/test_refee_energy_accounting.py` passed.
- `git diff --check` passed.

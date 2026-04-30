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

### Post-fix warning: staking no-delegated-accounts branch over-provisioned

**Files changed:**
- `app/tasks.py`

The follow-up reviewer found that the legacy staking path still used full
`energy_needed` when the onetime account had partial usable energy but no
`fromAccounts` entry. This was validated with a RED regression test: with
`EnergyLimit=100000`, `EnergyUsed=90000`, and `energy_needed=50000`, the provider
received `50000` instead of the missing `40000`.

The no-`fromAccounts` staking branch now provisions
`energy_needed - onetime_energy_available`, matching the additional-delegation
branch and avoiding unnecessary staking requirements.

### Live API limit: re:Fee energy order has a 30000 minimum

**Files changed:**
- `app/refee.py`
- `app/energy_provider.py`
- `README.md`

The live re:Fee API was probed with the test key using `amount=1` for
`resource=energy`. It returned HTTP `422` with:
`The quantity of Resource.ENERGY must be between 30000 and 5000000`.

`RefeeConfig` now has `min_energy_order_amount=30000` by default.
`RefeeEnergyProvider.acquire(...)` still sizes orders from the missing energy
delta and the configured overprovision factor, but applies this live API minimum
as a floor so small top-ups do not create invalid re:Fee orders.

## Regression Coverage

**Files added:**
- `tests/test_refee_energy_accounting.py`

The regression tests cover:
- partially used delegated energy triggers a re:Fee top-up for the missing delta;
- existing delegated bandwidth does not block re:Fee energy rental;
- re:Fee orders use the missing delta but verify the full required available energy.
- staking mode with partial usable energy and no delegated accounts provisions
  only the missing energy delta.
- small re:Fee top-ups below the live API minimum are raised to
  `min_energy_order_amount`.

## Verification

- RED: `/tmp/tron-shkeeper-py312-venv/bin/python -m unittest tests.test_refee_energy_accounting.RefeeEnergyAccountingTests.test_staking_acquires_missing_energy_when_no_delegated_accounts_exist` failed with `50000 != 40000`.
- GREEN: the same targeted regression test passed after the staking branch fix.
- Live API probe: `POST /api/rent_resource/orders` with `amount=1`,
  `resource=energy`, and `duration_label=1h` returned HTTP `422`:
  `The quantity of Resource.ENERGY must be between 30000 and 5000000`.
- RED: `/tmp/tron-shkeeper-py312-venv/bin/python -m unittest tests.test_refee_energy_accounting.RefeeEnergyAccountingTests.test_refee_provider_applies_live_api_minimum_energy_order_amount` failed with `10500 != 30000`.
- GREEN: the same targeted re:Fee minimum regression test passed after applying
  the order floor.
- `/tmp/tron-shkeeper-py312-venv/bin/python -m unittest tests.test_refee_energy_accounting` passed.
- `/tmp/tron-shkeeper-py312-venv/bin/python -m unittest discover -s tests` passed.
- `/tmp/tron-shkeeper-py312-venv/bin/python -m py_compile app/refee.py app/config.py app/energy_provider.py app/tasks.py app/utils.py tests/test_phase2_review_fixes.py tests/test_refee_bandwidth_guard.py tests/test_refee_energy_accounting.py` passed.
- `git diff --check` passed.

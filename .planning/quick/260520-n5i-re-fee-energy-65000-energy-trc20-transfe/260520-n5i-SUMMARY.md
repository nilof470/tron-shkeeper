---
status: complete
type: quick
completed: 2026-05-20
code_commit: 21319cc
---

# Quick Task 260520-n5i: Prevent Duplicate re:Fee Energy Rental - Summary

## Outcome

Fixed the duplicate fixed re:Fee energy rental path in commit `21319cc`.
A fixed `65,000` energy
order now treats available on-chain energy down to `64,500` as acceptable, so
the next balance scan will not rent another `65,000` energy just because the
provider/fullnode reports slightly less than the requested amount.

## Root Cause

Production logs showed that the first fixed re:Fee order requested `65,000`
energy and settled as `64,999` available energy. The provider post-check
compared that value against the fixed order amount and returned `False`, which
left the sweep queued for the next scan. The next scan saw the same deposit and
ordered another fixed `65,000` energy.

The successful transfer later used `64,285` energy, so the first order was
actually sufficient for the observed TRC20 sweep.

## Changes

- `RefeeProvider.acquire_energy` keeps `REFEE_FIXED_ENERGY_ORDER_AMOUNT` as the
  fixed order size.
- In fixed-order mode, the provider now verifies against
  `fixed_order_amount - 500`, so `65,000` requires at least `64,500` available
  energy.
- Existing available energy at or above the same threshold skips a new paid
  fixed-order rental.
- Dynamic mode with `REFEE_FIXED_ENERGY_ORDER_AMOUNT=0` is unchanged.

## Tests

RED observed before the final tolerance change:
- `64,500` available energy after a fixed `65,000` order was rejected.

GREEN verification:
- `uv run --python 3.12 --with-requirements requirements.txt python -m unittest tests.test_refee_energy_accounting.RefeeEnergyAccountingTests.test_refee_provider_accepts_fixed_order_tolerance_lower_bound tests.test_refee_energy_accounting.RefeeEnergyAccountingTests.test_refee_provider_rejects_fixed_order_below_tolerance_lower_bound -v` passed.
- `uv run --python 3.12 --with-requirements requirements.txt python -m unittest tests.test_refee_energy_accounting -v` passed.
- `uv run --python 3.12 --with-requirements requirements.txt python -m unittest discover -s tests -v` passed with 61 tests.
- `git diff --check` passed.

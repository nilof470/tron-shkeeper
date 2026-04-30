---
phase: 02-refee-energyprovider-dispatch
plan: 03
subsystem: sweep-flow
tags: [tasks, fallback, refee]
key-files:
  modified:
    - app/tasks.py
requirements-completed:
  - REQ-002
  - REQ-005
  - REQ-007
  - REQ-009
  - REQ-102
completed: 2026-04-30
---

# Phase 2 Plan 03: Sweep Wiring Summary

Wired `transfer_trc20_from` so `ENERGY_SOURCE=refee` enters energy-provider mode
without running staking-only checks, and added re:Fee failure fallback to the
existing TRX-burn path.

## What Changed

- Extracted `_fund_onetime_for_trc20_burn(...)` from the existing burn branch.
- Added `use_refee_energy_provider`, `use_staking_energy_provider`, and
  `use_energy_provider` flags.
- Kept staking-only energy-delegator bandwidth checks behind
  `use_staking_energy_provider`.
- Added re:Fee acquire failure fallback when
  `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT=true`.
- Preserved staking acquire failure behavior: it still returns without broadcasting.
- Switched post-transfer release guard to `if use_energy_provider:`.

## Verification

- `compileall app/tasks.py` passed under Python 3.12 temp venv.
- Acceptance greps from `02-03-PLAN.md` passed.
- `git diff --check` passed.

## Deviations from Plan

Added one extra INFO log line, `Using energy provider source: ...`, to make the
new dispatch decision visible in operator logs. This supports REQ-101 and does not
change control flow.

## Next

Ready for `02-04-PLAN.md`: smoke verification and live-spike status recording.

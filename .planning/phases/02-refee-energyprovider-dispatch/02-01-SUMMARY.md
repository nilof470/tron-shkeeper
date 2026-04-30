---
phase: 02-refee-energyprovider-dispatch
plan: 01
subsystem: config
tags: [refee, config, pydantic]
key-files:
  created:
    - app/refee.py
  modified:
    - app/config.py
requirements-completed:
  - REQ-001
  - REQ-011
  - REQ-012
  - REQ-014
completed: 2026-04-30
---

# Phase 2 Plan 01: Config Surface Summary

Added the re:Fee configuration surface without changing runtime behavior.

## What Changed

- Created `app/refee.py` with `RefeeConfig`.
- Added `ENERGY_SOURCE: Literal["staking", "refee"] = "staking"`.
- Added `REFEE: Json[RefeeConfig] | None = None`.
- Added a `model_validator(mode="after")` fail-fast check for `ENERGY_SOURCE="refee"` without `REFEE`.

## Verification

- `compileall app/refee.py app/config.py` passed under Python 3.12 temp venv.
- Acceptance greps from `02-01-PLAN.md` passed.

## Deviations from Plan

None - plan executed exactly as written.

## Next

Ready for `02-02-PLAN.md`: implement `RefeeEnergyProvider` and factory dispatch.

---
phase: 01-energyprovider-abstraction
plan: 01
subsystem: payments
tags: [tron, energy, staking, provider]
requires: []
provides:
  - EnergyProvider abstract contract for energy acquisition/release
  - StakingEnergyProvider concrete implementation using existing delegate-v2 staking flow
  - get_energy_provider factory returning staking provider for Phase 1
affects: [energy-provider, transfer_trc20_from, refee-provider]
tech-stack:
  added: []
  patterns: [provider-abstraction, lazy-import-for-task-cycle]
key-files:
  created: [app/energy_provider.py]
  modified: []
key-decisions:
  - "Keep Phase 1 factory unconditionally on StakingEnergyProvider; ENERGY_SOURCE branching remains Phase 2 scope."
  - "Use minimum_energy_required keyword-only argument so staking top-ups can delegate only the delta while checking total required energy."
patterns-established:
  - "Energy providers acquire energy units, not precomputed SUN amounts."
  - "Provider release lazily imports app.tasks.undelegate_energy to avoid an app.tasks <-> app.energy_provider cycle."
requirements-completed: [REQ-004, REQ-006]
duration: 20min
completed: 2026-04-30
---

# Phase 1 Plan 01 Summary

**EnergyProvider abstraction with staking-backed acquire/release preserving the existing delegate-v2 flow**

## Performance

- **Duration:** 20 min
- **Started:** 2026-04-30T12:23:00Z
- **Completed:** 2026-04-30T12:43:11Z
- **Tasks:** 1
- **Files modified:** 1

## Accomplishments

- Added `app/energy_provider.py` with `EnergyProvider`, `StakingEnergyProvider`, and `get_energy_provider()`.
- Lifted staking delegation logic into `StakingEnergyProvider.acquire()` while preserving the final `EnergyLimit >= energy_needed` recheck through `minimum_energy_required`.
- Added `StakingEnergyProvider.release()` with the same sync-vs-Celery dispatch semantics as the existing `tasks.py` undelegation block.

## Task Commits

1. **Task 1: Create EnergyProvider module** - `7da9b01` (feat)

## Files Created/Modified

- `app/energy_provider.py` - Provider contract, staking implementation, and Phase 1 factory.

## Decisions Made

- Followed the plan's Phase 1 boundary: no `ENERGY_SOURCE`, no re:Fee provider, no dependency changes.
- Used an absolute lazy import (`from app.tasks import undelegate_energy`) inside `release()` because `tasks.py` will import the provider in Plan 02.

## Deviations from Plan

None - plan executed as written.

## Issues Encountered

- The repository `.venv` uses Python 3.9.6 and cannot import the existing codebase because `app/config.py` already uses Python 3.10+ union syntax. Verification was run in a temporary Python 3.12 venv at `/tmp/tron-shkeeper-py312-venv`.

## Verification

- `PYTHONPYCACHEPREFIX=/tmp/tron-shkeeper-pycache /tmp/tron-shkeeper-py312-venv/bin/python -m compileall app/energy_provider.py`
- Provider signature/factory import check with Python 3.12: passed.
- `EnergyProvider` abstract method set check: passed.

## Next Phase Readiness

Plan 02 can now import `get_energy_provider()` and replace the inline staking closures in `transfer_trc20_from`.

---
*Phase: 01-energyprovider-abstraction*
*Completed: 2026-04-30*

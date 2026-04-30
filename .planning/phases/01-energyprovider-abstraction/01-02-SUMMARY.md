---
phase: 01-energyprovider-abstraction
plan: 02
subsystem: payments
tags: [tron, energy, staking, provider, celery]
requires:
  - phase: 01-energyprovider-abstraction/01-01
    provides: EnergyProvider abstraction and StakingEnergyProvider implementation
provides:
  - transfer_trc20_from delegates energy through get_energy_provider()
  - post-transfer undelegation dispatch uses provider.release()
affects: [transfer_trc20_from, energy-provider, refee-provider]
tech-stack:
  added: []
  patterns: [provider-abstraction-call-site, lazy-release-dispatch]
key-files:
  created: []
  modified: [app/tasks.py]
key-decisions:
  - "Bind provider once at the top of the ENERGY_DELEGATION_MODE branch so acquire and release share the same abstraction point."
  - "Keep energy_to_provision in energy units; provider owns SUN conversion for staking."
patterns-established:
  - "transfer_trc20_from decides how much energy is needed; providers decide how to provision it."
  - "Release remains guarded by config.ENERGY_DELEGATION_MODE in the caller."
requirements-completed: [REQ-005, REQ-006]
duration: 4min
completed: 2026-04-30
---

# Phase 1 Plan 02 Summary

**transfer_trc20_from now uses the EnergyProvider abstraction for staking acquire/release**

## Performance

- **Duration:** 4 min
- **Started:** 2026-04-30T12:43:11Z
- **Completed:** 2026-04-30T12:47:04Z
- **Tasks:** 1
- **Files modified:** 1

## Accomplishments

- Removed the nested `calc_sun_for_energy_delegation` and `delegate_energy` closures from `transfer_trc20_from`.
- Added `get_energy_provider()` binding inside the `ENERGY_DELEGATION_MODE` branch.
- Replaced staking delegation with `provider.acquire(..., minimum_energy_required=energy_needed)`.
- Replaced post-transfer undelegation dispatch with `provider.release(onetime_publ_key)`.

## Task Commits

1. **Task 1: Wire EnergyProvider into transfer_trc20_from** - `d8a6ae3` (refactor)

## Diff Statistics

- `app/tasks.py`: 20 insertions, 89 deletions.
- Net file size delta: 69 lines removed.
- `app/config.py`: byte-identical to HEAD at the time of execution.
- `requirements.txt`: byte-identical to HEAD at the time of execution.

## Intentional Log-Line Changes

- Plan 01 changed the post-delegation diagnostic from `onetime_publ_key=...` to `receiver=...` inside `StakingEnergyProvider.acquire()` because the closure variable became a method parameter.
- Plan 02 replaced the caller-side SUN log (`Delegating ... TRX to ...`) with `Requesting energy provider to provision N energy on ...`; SUN conversion now belongs to the staking provider.

## Top-Up Preservation

The additional-delegation path passes `energy_diff` as `energy_to_provision`, but still passes full transfer-wide `energy_needed` as `minimum_energy_required`. This preserves the original closure behavior: after delegation, staking rechecks `EnergyLimit >= energy_needed`, not just `EnergyLimit >= energy_diff`.

## Defensive Edge

`energy_diff <= 0` is unreachable in the current control flow because the branch is entered only when `onetime_energy_available < energy_needed`. The warning branch remains, and the final `if energy_to_provision > 0:` guard prevents a nonsensical zero/negative acquire if a future change makes that branch reachable.

## Verification

- AST check confirmed both nested closures are gone from `transfer_trc20_from`.
- AST check confirmed `undelegate_energy` and `transfer_trx_from` remain module-level and unchanged.
- AST/source comparison confirmed the TRX-burn else branch is unchanged.
- Provider wiring checks confirmed one `provider = get_energy_provider()`, one `provider.release(...)`, and `minimum_energy_required=energy_needed`.
- Python 3.12 import check for `app.tasks` passed after creating the standard gitignored `data/database.db` schema required by the existing import-time `Wallet.main_account` query.

## Next Phase Readiness

Plan 03 owns smoke verification and human/operator signoff for the refactor.

---
*Phase: 01-energyprovider-abstraction*
*Completed: 2026-04-30*

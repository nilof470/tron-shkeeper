# Project State

**Last updated:** 2026-04-30

## Current position

Phase 1 execution in progress on `gsd-phase-1-energyprovider-abstraction`. Plan 01 is complete: `app/energy_provider.py` now contains the EnergyProvider abstraction and staking-backed implementation. Plan 02 is next.

## Memory across sessions

This GSD project (`tron-shkeeper/.planning/`) is the implementation site. Architectural decisions and economics are in the **companion** project at `/Users/test/PycharmProjects/shkeeper.io/.planning/spikes/` (spikes 001 and 002 done; 003 and 004 pending). Don't re-derive what's in those spikes — read them.

## Active decisions

- Mode: `rent_resource` 1h via re:Fee per sweep. Locked. Other modes (`always_charged`, `auto_charging`) are rejected.
- Integration site: this repo (`tron-shkeeper`), `app/tasks.py:354` and `app/energy_provider.py` (new file).
- Backward compatibility: existing freeze-v2 path preserved as `StakingEnergyProvider`, default `ENERGY_SOURCE=staking`.
- Default `REFEE_RENT_DURATION_LABEL=1h` per spike 001 economics (cheapest tier for our usage profile).
- Default `REFEE_RENT_ENERGY_OVERPROVISION_FACTOR=1.05` (5% safety margin over chain-estimated energy).
- Default `REFEE_RENT_TIMEOUT_SEC=60` and `REFEE_RENT_POLL_INTERVAL_SEC=2.0` (placeholder until spike 003 measures real latency).
- Idempotency: rely on existing `EnergyLimit ≥ energy_needed` check at `app/tasks.py:297-303`. No external_id, no new state.
- Repo: `nilof470/tron-shkeeper` fork. Upstream `vsys-host/tron-shkeeper` push is disabled locally. Personal fork — no upstream PR.

## Open questions (deferred to spike 003)

- Status field name in JSON response (assumed `"status"` per OpenAPI; verify on live response).
- Realistic latency for `pending → delegated` (defaults assume up to 60s; tighten or loosen after measurement).
- Refund behavior on `failed`/`insufficient_funds` — does balance return to user account?
- Error body shape (assume free-form text; tighten if structured).
- Rate limit headers (assume polling at 2s is fine; back off if 429 observed).

## Recent work

- 2026-04-30: spikes 001 and 002 completed in `shkeeper.io` repo. Architectural design verified down to `app/tasks.py:354`.
- 2026-04-30: this `.planning/` scaffolded via shortcut B.
- 2026-04-30: `/gsd-plan-phase 1` reviewed existing Phase 1 plans and corrected the EnergyProvider `acquire` contract so staking top-ups delegate `energy_diff` while still verifying full `energy_needed`.
- 2026-04-30: `/gsd-execute-phase 1` completed Plan 01. Commit `7da9b01` added `app/energy_provider.py`; summary written to `.planning/phases/01-energyprovider-abstraction/01-01-SUMMARY.md`.

## Repo state

- Branch: `gsd-phase-1-energyprovider-abstraction`.
- Planning baseline is committed in `0df470f`.
- Plan 01 code is committed in `7da9b01`.

## Next action

Continue `/gsd-execute-phase 1` with Plan 02: wire `transfer_trc20_from` to `get_energy_provider()`.

# Project State

**Last updated:** 2026-04-30

## Current position

Phase 1 is complete on `gsd-phase-1-energyprovider-abstraction`. Plans 01-03 are complete; structural checks, Mode B local-stub smoke, post-review regression verification, and human approval are recorded. Phase 1.5 spike 003 has a committed runbook/probe in the companion repo, but live execution is pending operator API key, topped-up balance, and a test TRON address. Phase 2 is planned and ready for review/execution.

## Memory across sessions

This GSD project (`tron-shkeeper/.planning/`) is the implementation site. Architectural decisions and economics are in the **companion** project at `/Users/test/PycharmProjects/shkeeper.io/.planning/spikes/` (spikes 001 and 002 done; 003 and 004 pending). Don't re-derive what's in those spikes — read them.

## Active decisions

- Mode: `rent_resource` 1h via re:Fee per sweep. Locked. Other modes (`always_charged`, `auto_charging`) are rejected.
- Integration site: this repo (`tron-shkeeper`), `app/tasks.py:354` and `app/energy_provider.py` (new file).
- Backward compatibility: existing freeze-v2 path preserved as `StakingEnergyProvider`, default `ENERGY_SOURCE=staking`.
- Config surface: `ENERGY_SOURCE: Literal["staking","refee"] = "staking"` plus nested `REFEE: Json[RefeeConfig] | None`.
- Default `REFEE.rent_duration_label=1h` per spike 001 economics (cheapest tier for our usage profile).
- Default `REFEE.energy_overprovision_factor=1.05` (5% safety margin over chain-estimated energy).
- Default `REFEE.timeout_sec=60` and `REFEE.poll_interval_sec=2.0` (placeholder until spike 003 measures real latency).
- Idempotency: rely on existing `EnergyLimit ≥ energy_needed` check at `app/tasks.py:297-303`. No external_id, no new state.
- Repo: `nilof470/tron-shkeeper` fork. Upstream `vsys-host/tron-shkeeper` push is disabled locally. Personal fork — no upstream PR.

## Open questions (deferred to spike 003)

- Live confirmation that the OpenAPI `status` field and lowercase values match production responses.
- Realistic latency for `pending → delegated` (defaults assume up to 60s; tighten or loosen after measurement).
- Refund behavior on `failed`/`insufficient_funds` — does balance return to user account?
- Error body shape (assume free-form text; tighten if structured).
- Rate limit headers (assume polling at 2s is fine; back off if 429 observed).

## Recent work

- 2026-04-30: spikes 001 and 002 completed in `shkeeper.io` repo. Architectural design verified down to `app/tasks.py:354`.
- 2026-04-30: this `.planning/` scaffolded via shortcut B.
- 2026-04-30: `/gsd-plan-phase 1` reviewed existing Phase 1 plans and corrected the EnergyProvider `acquire` contract so staking top-ups delegate `energy_diff` while still verifying full `energy_needed`.
- 2026-04-30: `/gsd-execute-phase 1` completed Plan 01. Commit `7da9b01` added `app/energy_provider.py`; summary written to `.planning/phases/01-energyprovider-abstraction/01-01-SUMMARY.md`.
- 2026-04-30: `/gsd-execute-phase 1` completed Plan 02. Commit `d8a6ae3` wired `transfer_trc20_from` to `get_energy_provider()`, removed inline staking closures, and preserved staking top-up recheck semantics.
- 2026-04-30: Plan 03 structural verification and Mode B local-stub smoke were recorded in `.planning/phases/01-energyprovider-abstraction/01-03-SMOKE.md`. Status is pending human approval.
- 2026-04-30: Code review findings fixed. Commit `f68e27f` makes `StakingEnergyProvider` reuse the sweep-selected TRON client; commit `4902690` removes stale `json`/`math` imports from `app/tasks.py`. Regression smoke confirmed `ConnectionManager.client()` is called once during the sweep.
- 2026-04-30: Human approval recorded for Phase 1. `01-03-SUMMARY.md` created; Phase 1 is complete.
- 2026-04-30: Companion spike 003 runbook/probe prepared in `shkeeper.io` commit `7f4aff5`; live order remains pending credentials/top-up.
- 2026-04-30: `/gsd-plan-phase 2` completed inline: context, research, pattern map, and four executable plans under `.planning/phases/02-refee-energyprovider-dispatch/`.

## Repo state

- Branch: `gsd-phase-1-energyprovider-abstraction`.
- Planning baseline is committed in `0df470f`.
- Plan 01 code is committed in `7da9b01`.
- Plan 02 code is committed in `d8a6ae3`.
- Plan 03 smoke artifact is committed in `d3be929` and updated with post-review fix verification.
- Review fixes are committed in `f68e27f` and `4902690`; `01-REVIEW-FIX.md` is committed in `bb98bce`.
- Plan 03 summary and approval closure are committed in `487c566`.
- State sync after approval is committed in `13e7aae`.

## Next action

Recommended next action: review and run `$gsd-execute-phase 2`. During Plan 04, run companion spike 003 live if the operator provides `REFEE_API_KEY`, topped-up re:Fee balance, and `REFEE_TEST_TRON_ADDRESS`; otherwise record live spike pending and keep timeout defaults provisional.

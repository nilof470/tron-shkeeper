# Project State

**Last updated:** 2026-04-30

## Current position

Phase 1 is complete on `gsd-phase-1-energyprovider-abstraction`. Plans 01-03 are complete; structural checks, Mode B local-stub smoke, post-review regression verification, and human approval are recorded. Phase 1.5 spike 003 is live-validated in the companion repo: a test re:Fee `rent_resource` order reached `delegated` and on-chain energy was confirmed. Phase 2 is implemented, code-reviewed, hardened, and structurally verified with mocked re:Fee smokes plus live order lifecycle validation. Phase 3 is complete: a real USDT-TRC20 sweep used re:Fee energy and moved `3 USDT` from the generated onetime wallet to fee_deposit with zero TRX burned for the USDT transfer, and `README.md` now documents re:Fee setup, activation, bandwidth, retry behavior, and Helm/container env overrides.

## Memory across sessions

This GSD project (`tron-shkeeper/.planning/`) is the implementation site. Architectural decisions and economics are in the **companion** project at `/Users/test/PycharmProjects/shkeeper.io/.planning/spikes/` (spikes 001, 002, and 003 done; 004 pending). Don't re-derive what's in those spikes — read them.

## Active decisions

- Mode: `rent_resource` 1h via re:Fee per sweep. Locked. Other modes (`always_charged`, `auto_charging`) are rejected.
- Integration site: this repo (`tron-shkeeper`), `app/tasks.py:354` and `app/energy_provider.py` (new file).
- Backward compatibility: existing freeze-v2 path preserved as `StakingEnergyProvider`, default `ENERGY_SOURCE=staking`.
- Config surface: `ENERGY_SOURCE: Literal["staking","refee"] = "staking"` plus nested `REFEE: Json[RefeeConfig] | None`.
- Default `REFEE.rent_duration_label=1h` per spike 001 economics (cheapest tier for our usage profile).
- Default `REFEE.energy_overprovision_factor=1.05` (5% safety margin over chain-estimated energy).
- Default `REFEE.min_energy_order_amount=30000`, matching the live re:Fee `resource=energy` order minimum observed from the API.
- Default `REFEE.timeout_sec=60` and `REFEE.poll_interval_sec=2.0` remain conservative after spike 003 measured live delegation at 4.933s.
- Idempotency: rely on existing `EnergyLimit ≥ energy_needed` check at `app/tasks.py:297-303`. No external_id, no new state.
- Repo: `nilof470/tron-shkeeper` fork. Upstream `vsys-host/tron-shkeeper` push is disabled locally. Personal fork — no upstream PR.

## Open questions

- Refund behavior on `failed`/`insufficient_funds` — does balance return to user account?
- Error body shape (assume free-form text; tighten if structured).
- Final branch review before ship/merge.

## Recent work

- 2026-04-30: spikes 001 and 002 completed in `shkeeper.io` repo. Architectural design verified down to `app/tasks.py:354`.
- 2026-04-30: this `.planning/` scaffolded via shortcut B.
- 2026-04-30: `/gsd-plan-phase 1` reviewed existing Phase 1 plans and corrected the EnergyProvider `acquire` contract so staking top-ups delegate `energy_diff` while still verifying full `energy_needed`.
- 2026-04-30: `/gsd-execute-phase 1` completed Plan 01. Commit `7da9b01` added `app/energy_provider.py`; summary written to `.planning/phases/01-energyprovider-abstraction/01-01-SUMMARY.md`.
- 2026-04-30: `/gsd-execute-phase 1` completed Plan 02. Commit `d8a6ae3` wired `transfer_trc20_from` to `get_energy_provider()`, removed inline staking closures, and preserved staking top-up recheck semantics.
- 2026-04-30: Plan 03 structural verification and Mode B local-stub smoke were recorded in `.planning/phases/01-energyprovider-abstraction/01-03-SMOKE.md`. Status is pending human approval.
- 2026-04-30: Code review findings fixed. Commit `f68e27f` makes `StakingEnergyProvider` reuse the sweep-selected TRON client; commit `4902690` removes stale `json`/`math` imports from `app/tasks.py`. Regression smoke confirmed `ConnectionManager.client()` is called once during the sweep.
- 2026-04-30: Human approval recorded for Phase 1. `01-03-SUMMARY.md` created; Phase 1 is complete.
- 2026-04-30: Companion spike 003 runbook/probe prepared in `shkeeper.io` commit `7f4aff5`; live order validation was completed later with operator test credentials/top-up.
- 2026-04-30: `/gsd-plan-phase 2` completed inline: context, research, pattern map, and four executable plans under `.planning/phases/02-refee-energyprovider-dispatch/`.
- 2026-04-30: `/gsd-execute-phase 2` completed inline through Plan 04. Commits `567a75e`, `e9e2d57`, `2a262fe`, and `0eae4c7` add config, `RefeeEnergyProvider`, sweep fallback wiring, and smoke verification.
- 2026-04-30: Phase 2 code review fixed re:Fee acquire semantics: only `delegated` is safe before broadcast, malformed API JSON/fullnode check errors return `False`, and invalid REFEE numeric config fails at startup. Review recorded in `02-REVIEW.md`.
- 2026-04-30: Post-review verification reran compile/import/config smokes plus mocked re:Fee happy path, `completed` rejection, and burn-fallback path. Main repo worktree is clean after commit `66cc4bb`.
- 2026-04-30: Phase 2 deep review findings fixed in commits `b83d004` and `0432641`: selected-client bandwidth checks, empty API key validation, and `SUCCESS_STATUSES` usage.
- 2026-04-30: Companion spike 003 live run succeeded with test API key and topped-up re:Fee balance: `pending -> delegated`, delegation latency `4.933s`, on-chain energy available `0 -> 64999`, no rate-limit headers observed. Probe needed browser-like User-Agent for urllib; production `requests` profile check returned HTTP 200.
- 2026-04-30: Phase 3 prep artifacts created. Generated sidecar-controlled onetime address `TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7` in `/tmp/tron-shkeeper-phase3-e2e/database.db`; initial check shows private key present, account inactive, `0` USDT, `0` TRX.
- 2026-04-30: Phase 3 live e2e succeeded. Onetime `TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7` swept `3 USDT` to fee_deposit `TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k`. Successful USDT tx: `9bdfabfee0c57508c0a58d1521c6f512ecb07f54eff219a8f56cf81f3b10634f`. The successful transfer used `130285` energy and `345` bandwidth; onetime ended with `0 USDT`, `0 TRX`.
- 2026-04-30: Live e2e exposed bandwidth as a separate prerequisite from re:Fee energy. Commit `3b550e8` moved the onetime bandwidth precheck before energy estimation/re:Fee rental, so low-bandwidth repeat sweeps stop before creating a paid re:Fee order.
- 2026-04-30: Production operator docs added in `README.md`: `ENERGY_SOURCE=refee`, `REFEE` JSON, activation/bandwidth behavior, burn fallback flags, Helm/container env examples, and live validation evidence.
- 2026-04-30: Final subagent review found three energy-accounting risks after the bandwidth follow-up. Fix applied: sweep/provider checks now use available energy (`EnergyLimit - EnergyUsed`), re:Fee mode ignores delegated-resource `fromAccounts` as an energy gate, and re:Fee top-up orders are sized from the missing energy delta.
- 2026-04-30: Follow-up post-fix review warning validated and fixed: staking mode with partial usable energy and no `fromAccounts` now delegates only the missing energy delta, covered by a RED/GREEN regression test.
- 2026-04-30: Live re:Fee API probe confirmed `resource=energy` order quantity must be `30000..5000000`; code now floors small re:Fee top-up orders at `REFEE.min_energy_order_amount=30000`.

## Repo state

- Branch: `gsd-phase-1-energyprovider-abstraction`.
- Planning baseline is committed in `0df470f`.
- Plan 01 code is committed in `7da9b01`.
- Plan 02 code is committed in `d8a6ae3`.
- Plan 03 smoke artifact is committed in `d3be929` and updated with post-review fix verification.
- Review fixes are committed in `f68e27f` and `4902690`; `01-REVIEW-FIX.md` is committed in `bb98bce`.
- Plan 03 summary and approval closure are committed in `487c566`.
- State sync after approval is committed in `13e7aae`.
- Phase 2 planning is committed in `e97bdf0`.
- Phase 2 code/smoke/review-fix commits: `567a75e`, `e9e2d57`, `2a262fe`, `0eae4c7`, `66cc4bb`, `655ec4b`, `b83d004`, `0432641`.
- Phase 3 prep/e2e commits: `1db2d97`, `c1c5876`, `174c975`, `3b550e8`.
- Final review follow-up is recorded in Phase 3 review artifacts.

## Next action

Recommended next action: run final branch review and then prepare the work for ship/merge.

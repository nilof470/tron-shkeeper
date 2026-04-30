# Roadmap

**Created:** 2026-04-30
**Total phases:** 3 (+ 1 verification spike between phases)

## Phase 1 — `EnergyProvider` abstraction with `StakingEnergyProvider`

**Status:** Complete — approved 2026-04-30.

**Goal:** Extract existing freeze-v2 / delegate-v2 logic from inline functions inside `transfer_trc20_from` into a clean `EnergyProvider` abstraction with a single concrete implementation (`StakingEnergyProvider`). No behavior change. This is a pure refactor that prepares for Phase 2.

**Covers:** REQ-004, REQ-005 (partial), REQ-006

**Deliverables:**
- New file `app/energy_provider.py` with `EnergyProvider` ABC + `StakingEnergyProvider` + `get_energy_provider()` factory.
- `app/tasks.py:transfer_trc20_from` modified: inner `calc_sun_for_energy_delegation` and `delegate_energy` removed; calls go through `get_energy_provider().acquire(...)` and `provider.release(...)`.
- All existing env vars and behavior preserved when `ENERGY_SOURCE` is unset (default to staking).

**Done when:**
- Default deployment (no new env vars set) executes a USDT-TRC20 sweep with energy delegation, behavior identical to current master.
- Smoke run via `celery_worker` shell or unit-style script confirms `transfer_trc20_from` completes without raising new errors on the existing happy path.

**Risk:** Low. Pure mechanical refactor with no new external dependencies.

## Phase 1.5 — Spike 003 (re:Fee live order lifecycle)

**Status:** Validated — live test order reached `delegated` on 2026-04-30.

**Goal:** Run live `POST /api/rent_resource/orders` + polling to confirm: (a) status field naming in JSON, (b) realistic latency for `pending → delegated`, (c) refund behavior on `failed` / `insufficient_funds`, (d) error body shape, (e) any rate-limit headers.

**Covers:** open questions in `.planning/spikes/002-tron-shkeeper-sidecar-recon/README.md` "Open Questions" section.

**Deliverables:**
- Spike 003 README and stdlib probe in companion `shkeeper.io` repo at `.planning/spikes/003-refee-rent-order-lifecycle/`.
- Concrete defaults for `REFEE.poll_interval_sec` and `REFEE.timeout_sec` based on observed latency: keep `2.0s` polling and `60s` timeout after live delegation at `4.933s`.
- Verified status field name and value casing for `RefeeEnergyProvider`: `status`, lowercase `pending -> delegated`.
- Verified on-chain energy arrival: available energy changed from `0` to `64999`.

**Risk:** Requires user to top up re:Fee balance (~3 TRX) and run a curl bundle.

**Out of band:** Can run in parallel with Phase 1 implementation.

## Phase 2 — `RefeeEnergyProvider` + dispatch

**Status:** Implemented and code-reviewed — structural/mocked smoke passed; live re:Fee order lifecycle validated; full sweep remains Phase 3.

**Goal:** Add the new `RefeeEnergyProvider` to `app/energy_provider.py`, wire it into the factory via the new `ENERGY_SOURCE` env var, add nested `REFEE` config to `Settings`, add the cross-field validator, and wire re:Fee fallback into `transfer_trc20_from`.

**Covers:** REQ-001, REQ-002, REQ-003, REQ-005 (full), REQ-007, REQ-008, REQ-009, REQ-010, REQ-011, REQ-012, REQ-013, REQ-014, REQ-015, REQ-016, REQ-101, REQ-102, REQ-103

**Deliverables:**
- `RefeeEnergyProvider` class with `acquire()` (POST + poll + on-chain verify) and `release()` (no-op).
- `Settings.ENERGY_SOURCE` and nested `REFEE: Json[RefeeConfig] | None` config with defaults.
- `model_validator(mode='after')` enforcing `REFEE` is set when `ENERGY_SOURCE=refee`.
- Logger calls at every lifecycle decision point (REQ-101).
- Fallback path in `transfer_trc20_from` when `acquire` returns False, gated by `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT` (REQ-007).

**Plan breakdown:**
- Wave 1: `02-01-PLAN.md` — config surface (`app/refee.py`, `app/config.py`).
- Wave 2: `02-02-PLAN.md` — `RefeeEnergyProvider` and factory dispatch.
- Wave 3: `02-03-PLAN.md` — `transfer_trc20_from` provider-mode and fallback wiring.
- Wave 4: `02-04-PLAN.md` — structural, mocked, and optional live smoke verification.

**Execution notes:**
- Plans 02-01 through 02-04 have summaries.
- Default staking import/compile smoke passed.
- Mocked re:Fee provider happy path and mocked re:Fee failure + TRX-burn fallback passed.
- Code review hardening applied: `completed` status is rejected pre-broadcast, malformed API/fullnode failures return `False`, and non-positive REFEE timing/factor config is invalid.
- Review fix pass applied: selected-client bandwidth checks, empty API key validation, and `SUCCESS_STATUSES` usage.
- Live `POST /api/rent_resource/orders` validated in Phase 1.5: HTTP 202, `pending -> delegated`, 4.933s delegation latency, on-chain energy visible.

**Done when:**
- `ENERGY_SOURCE=staking` (default): behavior identical to Phase 1 / upstream master.
- `ENERGY_SOURCE=refee` + valid API key + topped-up balance + a user-wallet with USDT and zero TRX: `transfer_trc20_from` sequence runs through `RefeeEnergyProvider.acquire`, gets back True after on-chain verify, broadcasts the USDT-TRC20 transfer, returns success.
- `ENERGY_SOURCE=refee` without `REFEE`: pydantic raises a clear validation error at process startup.
- Re:Fee failure (mocked or real) + `ALLOW_BURN_TRX_ON_PAYOUT=true`: falls back to TRX-burn path without raising.

**Risk:** Medium. Live API integration. Spike 003 closes the unknowns.

## Phase 3 — Live e2e validation + docs (was spike 004)

**Status:** Complete — live e2e passed and operator docs added.

**Goal:** Real end-to-end test on TRON mainnet: a fresh user-wallet with a small USDT balance, sidecar configured with `ENERGY_SOURCE=refee`, observe the full sweep with zero TRX burned. Update operator docs.

**Covers:** Verification of all REQ-* (live), plus operator-facing docs.

**Deliverables:**
- E2E test artifact (recorded log, tx hashes on tronscan, before/after balances): recorded in `03-01-SUMMARY.md`.
- Update to `tron-shkeeper/README.md` with a re:Fee setup section.
- Optional helm chart override snippet for `vsys-host/helm-charts` users (or note in README).
- Prepared runbook/helper under `.planning/phases/03-live-e2e-validation-docs/`.
- Generated test onetime address: `TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7`.
- Successful USDT transfer tx: `9bdfabfee0c57508c0a58d1521c6f512ecb07f54eff219a8f56cf81f3b10634f`.
- re:Fee delegation tx used by the sweep: `7e034daae3e0c2ab20c36602740b5a21386792765ac62e79763ba5c3031ab740`.

**Done when:**
- [x] One real USDT-TRC20 sweep completes via re:Fee with zero TRX burned for the USDT transfer, recorded with tx hashes.
- [x] Operator docs explain how to enable re:Fee in their helm `values.yaml`.

**Risk:** Low (assumes Phase 2 passed). Cost: ~5–10 USDT for the test sweep + ~3 TRX re:Fee balance.

## Phase ordering rationale

Phase 1 is a refactor — it can ship and run in production-like conditions before any re:Fee integration. This makes Phase 2 a smaller, focused diff. Phase 1.5 (spike 003) runs in parallel with Phase 1 because it's pure observation, no code dependency. Phase 2 needs spike 003 outputs for the timeout/latency knobs but can start scaffolding before. Phase 3 is the final live validation gate before merging to fork's master.

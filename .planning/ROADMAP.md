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

**Goal:** Run live `POST /api/rent_resource/orders` + polling to confirm: (a) status field naming in JSON, (b) realistic latency for `pending → delegated`, (c) refund behavior on `failed` / `insufficient_funds`, (d) error body shape, (e) any rate-limit headers.

**Covers:** open questions in `.planning/spikes/002-tron-shkeeper-sidecar-recon/README.md` "Open Questions" section.

**Deliverables:**
- Spike 003 README in companion `shkeeper.io` repo at `.planning/spikes/003-refee-rent-order-lifecycle/`.
- Concrete defaults for `REFEE_RENT_POLL_INTERVAL_SEC` and `REFEE_RENT_TIMEOUT_SEC` based on observed latency.
- Verified status field name and value casing for `RefeeEnergyProvider`.

**Risk:** Requires user to top up re:Fee balance (~3 TRX) and run a curl bundle.

**Out of band:** Can run in parallel with Phase 1 implementation.

## Phase 2 — `RefeeEnergyProvider` + dispatch

**Goal:** Add the new `RefeeEnergyProvider` to `app/energy_provider.py`, wire it into the factory via the new `ENERGY_SOURCE` env var, add the seven re:Fee env vars to `Settings`, add the cross-field validator.

**Covers:** REQ-001, REQ-002, REQ-003, REQ-005 (full), REQ-007, REQ-008, REQ-009, REQ-010, REQ-011, REQ-012, REQ-013

**Deliverables:**
- `RefeeEnergyProvider` class with `acquire()` (POST + poll + on-chain verify) and `release()` (no-op).
- `Settings.ENERGY_SOURCE` and 6 new `REFEE_*` fields with defaults.
- `model_validator(mode='after')` enforcing `REFEE_API_KEY` is set when `ENERGY_SOURCE=refee`.
- Logger calls at every lifecycle decision point (REQ-101).
- Fallback path in `transfer_trc20_from` when `acquire` returns False, gated by `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT` (REQ-007).

**Done when:**
- `ENERGY_SOURCE=staking` (default): behavior identical to Phase 1 / upstream master.
- `ENERGY_SOURCE=refee` + valid API key + topped-up balance + a user-wallet with USDT and zero TRX: `transfer_trc20_from` sequence runs through `RefeeEnergyProvider.acquire`, gets back True after on-chain verify, broadcasts the USDT-TRC20 transfer, returns success.
- `ENERGY_SOURCE=refee` without `REFEE_API_KEY`: pydantic raises a clear validation error at process startup.
- Re:Fee failure (mocked or real) + `ALLOW_BURN_TRX_ON_PAYOUT=true`: falls back to TRX-burn path without raising.

**Risk:** Medium. Live API integration. Spike 003 closes the unknowns.

## Phase 3 — Live e2e validation + docs (was spike 004)

**Goal:** Real end-to-end test on TRON mainnet: a fresh user-wallet with a small USDT balance, sidecar configured with `ENERGY_SOURCE=refee`, observe the full sweep with zero TRX burned. Update operator docs.

**Covers:** Verification of all REQ-* (live), plus operator-facing docs.

**Deliverables:**
- E2E test artifact (recorded log, tx hashes on tronscan, before/after balances).
- Update to `tron-shkeeper/README.md` with a re:Fee setup section.
- Optional helm chart override snippet for `vsys-host/helm-charts` users (or note in README).

**Done when:**
- One real USDT-TRC20 sweep completes via re:Fee with zero TRX burned, recorded with tx hashes.
- Operator docs explain how to enable re:Fee in their helm `values.yaml`.

**Risk:** Low (assumes Phase 2 passed). Cost: ~5–10 USDT for the test sweep + ~3 TRX re:Fee balance.

## Phase ordering rationale

Phase 1 is a refactor — it can ship and run in production-like conditions before any re:Fee integration. This makes Phase 2 a smaller, focused diff. Phase 1.5 (spike 003) runs in parallel with Phase 1 because it's pure observation, no code dependency. Phase 2 needs spike 003 outputs for the timeout/latency knobs but can start scaffolding before. Phase 3 is the final live validation gate before merging to fork's master.

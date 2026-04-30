# Phase 2: RefeeEnergyProvider + Dispatch - Context

**Gathered:** 2026-04-30
**Status:** Ready for planning
**Source:** Roadmap + REQUIREMENTS.md + Phase 1 implementation + companion spikes 002/003

<domain>
## Phase Boundary

Phase 2 adds the re:Fee energy-rental implementation behind the Phase 1
`EnergyProvider` abstraction. It covers config, provider implementation,
factory dispatch, `transfer_trc20_from` wiring, fallback to the existing TRX-burn
flow, and non-live verification. It does not update operator docs or run the final
mainnet USDT sweep; that remains Phase 3.
</domain>

<decisions>
## Implementation Decisions

### D-01 Energy source selector
- Add `ENERGY_SOURCE: Literal["staking", "refee"] = "staking"` to `Settings`.
- Default `"staking"` preserves the Phase 1/default behavior.
- When `ENERGY_SOURCE="refee"`, the sweep must use `RefeeEnergyProvider` even if
  legacy `ENERGY_DELEGATION_MODE` is false.

### D-02 re:Fee config shape
- Add `REFEE: Json[RefeeConfig] | None = None` to `Settings`.
- Define `RefeeConfig` in `app/refee.py`.
- Required secret is `api_key: SecretStr`; defaults are:
  - `api_base_url = "https://api.refee.bot/v2"`
  - `rent_duration_label = "1h"`
  - `energy_overprovision_factor = Decimal("1.05")`
  - `poll_interval_sec = 2.0`
  - `timeout_sec = 60`
- Add a `model_validator(mode="after")` that raises when
  `ENERGY_SOURCE="refee"` and `REFEE is None`.

### D-03 re:Fee order contract
- `POST /api/rent_resource/orders` returns HTTP `202` on accepted order.
- Request body is exactly:
  `{"address": receiver, "amount": amount, "resource": "energy", "duration_label": config.rent_duration_label}`.
- Response order id field is `id`.
- Poll path is `GET /api/rent_resource/orders/{id}`.
- Status field is `status`; success starts at `delegated`.
- Terminal failures are `failed`, `insufficient_funds`, and `canceled`.
- The create schema has no `external_id` or idempotency key.

### D-04 Multi-server client continuity
- `RefeeEnergyProvider` must accept `tron_client` in `__init__`, like
  `StakingEnergyProvider`.
- `transfer_trc20_from` must keep passing the already selected `tron_client` into
  `get_energy_provider(tron_client=tron_client)` so one sweep does not switch
  fullnodes.

### D-05 Staking-only checks stay staking-only
- The existing energy-delegator bandwidth check is only valid for staking because
  re:Fee does not broadcast delegate/undelegate transactions from our energy
  delegator account.
- Phase 2 must guard that check with `use_staking_energy_provider`.

### D-06 Fallback semantics
- re:Fee acquire failures return `False`.
- If `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT=true`, re:Fee acquire
  failure falls back to the existing TRX-burn funding path.
- Staking acquire failure keeps existing behavior: return without broadcasting.
- TRX-burn fallback must skip the post-acquire free-bandwidth check, because the
  funded TRX is the fallback that lets the transfer burn resources.

### D-07 Release semantics
- `StakingEnergyProvider.release` remains the only path that schedules
  `undelegate_energy`.
- `RefeeEnergyProvider.release` is a logged no-op.

### D-08 Live spike state
- Companion spike 003 has a committed runbook/probe but no live result yet.
- Phase 2 can implement against the OpenAPI contract, but verification must keep
  the latency/timeout defaults provisional until the live probe is run.
</decisions>

<canonical_refs>
## Canonical References

### Planning
- `.planning/ROADMAP.md` - Phase 2 goal, deliverables, done-when checks.
- `.planning/REQUIREMENTS.md` - locked REQ-001 through REQ-016 and REQ-101 through REQ-103.
- `.planning/STATE.md` - current branch, Phase 1 completion, live-spike status.

### Phase 1 outputs
- `.planning/phases/01-energyprovider-abstraction/01-01-SUMMARY.md` - provider abstraction created.
- `.planning/phases/01-energyprovider-abstraction/01-02-SUMMARY.md` - `transfer_trc20_from` wiring state.
- `.planning/phases/01-energyprovider-abstraction/01-REVIEW-FIX.md` - selected `tron_client` regression fixed.
- `app/energy_provider.py` - Phase 2 extension point.
- `app/tasks.py` - sweep flow and fallback wiring target.

### Companion spikes
- `/Users/test/PycharmProjects/shkeeper.io/.planning/spikes/002-tron-shkeeper-sidecar-recon/README.md` - insertion point and architecture.
- `/Users/test/PycharmProjects/shkeeper.io/.planning/spikes/003-refee-rent-order-lifecycle/README.md` - OpenAPI contract and live probe.
- `/Users/test/PycharmProjects/shkeeper.io/docs/openapi-refeebot.json` - source schema for re:Fee fields/status values.
</canonical_refs>

<specifics>
## Specific Ideas

- Use the existing `requests==2.32.3`; do not add dependencies.
- Use `requests.post(..., timeout=10)` and `requests.get(..., timeout=10)` exactly.
- Never log `REFEE.api_key`.
- Keep `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT` as the fallback flag; do
  not add `REFEE_FALLBACK_ON_FAILURE`.
- Preserve the default staking path as the regression anchor.
</specifics>

<deferred>
## Deferred Ideas

- Prometheus metrics for re:Fee success/failure/latency.
- Admin endpoint for re:Fee balance.
- Operator README/helm docs.
- Mainnet e2e with real USDT sweep and zero TRX burn.
</deferred>

---

*Phase: 02-refee-energyprovider-dispatch*
*Context gathered: 2026-04-30 via inline gsd-plan-phase*

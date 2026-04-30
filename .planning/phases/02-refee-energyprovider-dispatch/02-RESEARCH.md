# Phase 2 Research: RefeeEnergyProvider + Dispatch

**Date:** 2026-04-30
**Mode:** inline research, no subagents spawned

## Summary

Phase 2 is an integration change across three surfaces:

1. `app/config.py` gains `ENERGY_SOURCE` and nested `REFEE` config.
2. `app/energy_provider.py` gains `RefeeEnergyProvider` and factory dispatch.
3. `app/tasks.py:transfer_trc20_from` treats re:Fee as an energy-provider mode,
   while keeping staking and burn behavior compatible.

The highest-risk part is not the HTTP call itself; it is preserving the sweep's
control flow. The re:Fee path must not run staking-only bandwidth checks, and
fallback must reuse the burn path without making the post-acquire free-bandwidth
check block the fallback.

## Codebase Findings

### Config pattern

`app/config.py` already uses Pydantic Settings v2 and nested config patterns:

- `MULTISERVER_CONFIG_JSON: Json[List[TronFullnode]] | None`
- `EXTERNAL_DRAIN_CONFIG: ExternalDrain | None`
- `@field_validator("EXTERNAL_DRAIN_CONFIG", mode="after")`

Phase 2 should add:

```python
ENERGY_SOURCE: Literal["staking", "refee"] = "staking"
REFEE: Json[RefeeConfig] | None = None
```

and an instance-level `@model_validator(mode="after")` to fail fast when re:Fee
is selected without credentials.

### Provider pattern

`StakingEnergyProvider` already accepts `tron_client=None` and falls back to
`ConnectionManager.client()`. The Phase 1 review found that provider code must
reuse the sweep-selected client in multi-server mode. `RefeeEnergyProvider`
must follow the same constructor pattern.

### Task wiring hazard

Current Phase 1 code enters the provider branch only when
`config.ENERGY_DELEGATION_MODE` is true. Phase 2 must introduce:

```python
use_refee_energy_provider = config.ENERGY_SOURCE == "refee"
use_staking_energy_provider = (
    config.ENERGY_SOURCE == "staking" and config.ENERGY_DELEGATION_MODE
)
use_energy_provider = use_refee_energy_provider or use_staking_energy_provider
```

Then `if config.ENERGY_DELEGATION_MODE:` becomes `if use_energy_provider:`.
The existing energy-delegator bandwidth block becomes `if use_staking_energy_provider:`.

### Fallback hazard

The existing TRX-burn path lives in the `else:` branch of `transfer_trc20_from`.
Fallback from re:Fee acquire failure cannot simply `return` to that branch because
control is already inside the provider branch. Extract the burn funding block into
a small helper returning `(ready: bool, tx_trx_res)` and call it from both:

- the original burn-mode branch;
- the re:Fee failure path when `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT`
  is true.

When fallback is used, skip the provider-branch free-bandwidth check before the
TRC-20 transfer. The fallback intentionally funds TRX so the transfer can burn.

## re:Fee API Contract

The companion repo OpenAPI snapshot confirms:

- Base URL default: `https://api.refee.bot/v2`
- Auth header: `X-API-Key`
- Create order: `POST /api/rent_resource/orders`
- Success: HTTP `202`
- Request: `address`, `amount`, `resource`, `duration_label`
- Poll: `GET /api/rent_resource/orders/{order_id}`
- Response id: `id`
- Status field: `status`
- Status enum: `pending`, `delegated`, `completed`, `failed`,
  `insufficient_funds`, `canceled`
- Error field: `error`
- No `external_id` or idempotency key

HTTP errors to convert to `False`: request timeout, connection error, JSON parse
failure, non-202 create response, poll non-200 response, terminal failure status,
and timeout before `delegated`.

## Validation Architecture

Plan 04 should create a smoke report that proves:

- default `ENERGY_SOURCE=staking` still imports and dispatches staking;
- `ENERGY_SOURCE=refee` without `REFEE` raises at Settings construction;
- a valid REFEE JSON parses into a config object with a secret API key;
- mocked re:Fee create/poll flow uses timeout `10`, sends expected JSON, polls
  by `id`, stops at `delegated`, and checks on-chain resources through the same
  sweep-selected `tron_client`;
- re:Fee failure with fallback enabled runs the burn helper and still reaches the
  token-transfer build path;
- no API key appears in logs/smoke artifacts.

Live latency remains unknown until companion spike 003 is run against a topped-up
re:Fee account. Keep defaults at `poll_interval_sec=2.0`, `timeout_sec=60`.

## Security Notes

- API key is a `SecretStr`; logs must never include `.get_secret_value()` or raw
  settings dumps.
- All re:Fee HTTP calls are bounded by `timeout=10`.
- No retry loop should create multiple orders after a create succeeds. Poll the
  returned `id`; if polling times out, return `False`.
- Idempotency remains on-chain: if a retry sees `EnergyLimit >= energy_needed`,
  the existing pre-check skips `acquire`.

# Requirements

**Created:** 2026-04-30
**Source:** distilled from user statements 2026-04-30 + spike 001/002 findings.

## Must-have (v1)

| ID | Requirement | Source |
|---|---|---|
| REQ-001 | A new env var `ENERGY_SOURCE` accepts `"staking"` or `"refee"`. Default `"staking"`. | Project decision |
| REQ-002 | When `ENERGY_SOURCE=refee`, the sweep flow (`transfer_trc20_from`) calls re:Fee `POST /api/rent_resource/orders` for the onetime user-wallet, polls until `status="delegated"`, then proceeds to broadcast the TRC-20 transfer. | Architecture spike 002 |
| REQ-003 | Order parameters: `address` = onetime public key; `amount` = `energy_needed * REFEE_RENT_ENERGY_OVERPROVISION_FACTOR` (default 1.05); `resource` = `"energy"`; `duration_label` = `"1h"`. | spike 001 economics; spike 002 code analysis |
| REQ-004 | A new module `app/energy_provider.py` defines the abstract `EnergyProvider` and two implementations: `StakingEnergyProvider` (lifted verbatim from current `delegate_energy` inner function) and `RefeeEnergyProvider`. Dispatch via `get_energy_provider()` factory. | spike 002 recommendation |
| REQ-005 | `transfer_trc20_from` in `app/tasks.py` is modified to use `provider.acquire(receiver, energy, account_resource)` at the existing delegation point (currently L354), and `provider.release(receiver)` at the existing undelegate point (currently L412-416). | spike 002 |
| REQ-006 | When `ENERGY_SOURCE=staking`, behavior is bit-identical to upstream master (existing freeze-v2 / delegate-v2 path executes unchanged). | Backward compatibility |
| REQ-007 | re:Fee API failures (HTTP 5xx, timeout exceeded, terminal status `failed/insufficient_funds/canceled`) cause `acquire` to return `False`. The sweep then either falls back to TRX-burn flow if `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT=true`, or skips this cycle (returns without broadcasting). | User intent: "fallback на burn TRX должен сохраняться" |
| REQ-008 | `RefeeEnergyProvider.acquire` performs an on-chain double-check via `tron_client.get_account_resource(receiver)` after re:Fee reports `delegated`, asserting `EnergyLimit ≥ energy_needed` before returning success. | Defense in depth — re:Fee bug or out-of-band undelegate |
| REQ-009 | `RefeeEnergyProvider.release` is a no-op (re:Fee auto-returns energy after the rent period). The existing `undelegate_energy` celery task is invoked only by `StakingEnergyProvider.release`. | spike 002 |
| REQ-010 | Polling interval `REFEE_RENT_POLL_INTERVAL_SEC` (default 2.0s), timeout `REFEE_RENT_TIMEOUT_SEC` (default 60s) configurable via env. | Operational tuning |
| REQ-011 | New env vars: `ENERGY_SOURCE` (top-level `Literal["staking","refee"]`, default `"staking"`) AND `REFEE` (top-level `Json[RefeeConfig] \| None`, default `None`). The `RefeeConfig` Pydantic model contains: `api_base_url` (default `https://api.refee.bot/v2`), `api_key: SecretStr`, `rent_duration_label: Literal["1h","1d","3d","7d","14d"]` (default `"1h"`), `energy_overprovision_factor: Decimal` (default 1.05), `poll_interval_sec: float` (default 2.0), `timeout_sec: int` (default 60). The nested-JSON shape mirrors the existing `EXTERNAL_DRAIN_CONFIG: ExternalDrain` pattern in `app/config.py:60` — operators already know this convention from the AML pipeline. | Configuration surface; pattern from `EXTERNAL_DRAIN_CONFIG` |
| REQ-012 | Pydantic `model_validator(mode='after')` on `Settings`: when `ENERGY_SOURCE=refee`, `REFEE` must be set (non-None). When `ENERGY_SOURCE=staking`, `REFEE` is ignored if set. Mirrors the `validate_external_drain_config_states` validator at `app/config.py:152-166`. | Fail fast on misconfiguration; pattern reuse |
| REQ-014 | The `RefeeConfig` Pydantic model is defined in a new file `app/refee.py` (or inside `app/energy_provider.py` if kept compact). Imported and used as a field type in `app/config.py:Settings`. | Module organization, mirrors `app/custom/aml/schemas.py` |
| REQ-015 | All HTTP calls to re:Fee API in `RefeeEnergyProvider` MUST set `timeout=10` on `requests.post/get`. Errors caught and converted to `acquire() -> False` rather than raising — caller decides fallback per `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT`. | Avoid AML antipattern of unbounded `requests.post(...)` |
| REQ-016 | re:Fee config values are read **lazily** inside `RefeeEnergyProvider.acquire()` (not bound at module import) — supports config reload and easier testing. Avoid module-level `ACCESS_URL = config.X` pattern from `aml-shkeeper/app/aml_bot_api.py`. | Avoid AML antipattern |
| REQ-013 | Idempotency on celery retry is implicit via the existing `EnergyLimit ≥ energy_needed` check at `app/tasks.py:297-303` — re-runs of `transfer_trc20_from` for the same onetime address skip the `acquire()` call when energy is already present. **No new code path required for this.** | spike 002 finding |

## Should-have (v1)

| ID | Requirement | Source |
|---|---|---|
| REQ-101 | Logging at `INFO` level for each lifecycle event (POST accepted, status transitions during polling, on-chain double-check result, fallback decisions). | Operability |
| REQ-102 | Re-use of existing `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT` flag for re:Fee fallback semantics (no new fallback flag). | Minimize config surface |
| REQ-103 | The `RefeeEnergyProvider` HTTP calls use a 10-second `requests` timeout to bound any single network operation. | Defensive coding |

## Nice-to-have (v2 / out of scope for v1)

| ID | Requirement |
|---|---|
| REQ-201 | Prometheus metrics: count of re:Fee acquires (success/fail), polling duration histogram, fallback count. |
| REQ-202 | Admin HTTP endpoint to query re:Fee balance from sidecar without exposing API key to shkeeper main. |
| REQ-203 | Cache `tariffs` calls so cost-aware logic can evaluate burn-TRX-vs-rent dynamically. |
| REQ-204 | Bandwidth rental support (currently free 1500/day suffices). |

## Excluded (rejected by user)

| ID | What | Why rejected |
|---|---|---|
| REJ-001 | re:Fee `always_charged` mode | "нет, никакого always charged. Нужно по api вызывать эндпоинт в момент автоматического sweep" |
| REJ-002 | re:Fee `auto_charging` mode | Cost ≈ 65% premium vs `rent_resource` 1h with no operational benefit for sweep-trigger model |
| REJ-003 | re:Fee for AML check | "мне нужен именно amlbot, он гораздо дешевле и я буду использовать его" |
| REJ-004 | re:Fee for address activation | Out of scope per "refee только для делегирования энергии" |

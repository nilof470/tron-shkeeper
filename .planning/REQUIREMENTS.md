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
| REQ-011 | New env vars: `ENERGY_SOURCE`, `REFEE_API_BASE_URL` (default `https://api.refee.bot/v2`), `REFEE_API_KEY` (SecretStr), `REFEE_RENT_DURATION_LABEL` (default `"1h"`), `REFEE_RENT_ENERGY_OVERPROVISION_FACTOR` (default 1.05), `REFEE_RENT_POLL_INTERVAL_SEC` (default 2.0), `REFEE_RENT_TIMEOUT_SEC` (default 60). All declared in `app/config.py:Settings`. | Configuration surface |
| REQ-012 | Pydantic validator on `Settings`: when `ENERGY_SOURCE=refee`, `REFEE_API_KEY` must be set (non-None). When `ENERGY_SOURCE=staking`, no re:Fee fields are required. | Fail fast on misconfiguration |
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

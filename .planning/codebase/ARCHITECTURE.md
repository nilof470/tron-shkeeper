# Architecture

**Analysis Date:** 2026-04-30
**Source:** spike 002 recon (full version at `shkeeper.io/.planning/spikes/002-tron-shkeeper-sidecar-recon/README.md`).

## System Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                       shkeeper.io main (peer)                         │
│   POST http://tron-shkeeper:6000/<symbol>/payout/<dest>/<amount>      │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │ HTTP Basic auth
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    tron-shkeeper sidecar (this repo)                  │
├──────────────┬───────────────────────────────────┬────────────────────┤
│  Flask API   │    Celery worker (background)     │  Block scanner     │
│  app/api/    │    app/tasks.py                   │  thread in run.py  │
│              │      • prepare_payout/payout      │                    │
│              │      • transfer_trc20_from        │                    │
│              │        (sweep — re:Fee target)    │                    │
│              │      • scan_accounts (cron 3600s) │                    │
│              │      • undelegate_energy          │                    │
└──────────────┴────────────────┬───────────────────┴────────────────────┘
                                │ tronpy
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│            TRON network (fullnode, configurable URL)                  │
│        FULLNODE_URL env var; multi-node failover                      │
└──────────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|---|---|---|
| Flask app factory | App init, blueprint registration | `app/__init__.py` |
| Payout API blueprint | HTTP entry for merchant payouts and balance reads | `app/api/payout.py` |
| Staking API blueprint | TRX freeze/unfreeze/delegate/undelegate admin endpoints | `app/api/staking.py` |
| Views API blueprint | Address gen, transaction lookup, dump, fee-deposit info | `app/api/views.py` |
| Metrics blueprint | Prometheus metrics | `app/api/metrics.py` |
| Wallet class | Per-symbol balance/transfer; default source = `fee_deposit` | `app/wallet.py` |
| `transfer_trc20_from` celery task | Sweep flow: drain a user-wallet's USDT to fee_deposit; **the function re:Fee plugs into** | `app/tasks.py:88` |
| `scan_accounts` periodic celery task | Triggers `transfer_trc20_from` for each user-wallet over threshold | `app/tasks.py:553` |
| `undelegate_energy` celery task | Reclaims delegated energy after sweep (no-op for re:Fee mode) | `app/tasks.py:421` |
| ConnectionManager | tronpy client pool with multi-fullnode failover | `app/connection_manager.py` |
| Energy delegator key wiring | Fee_deposit vs separate energy account | `app/utils.py:99` (`get_energy_delegator`) |
| Config / Settings | Single pydantic `BaseSettings` | `app/config.py` |
| AML alt path | Custom drain workflow (TRX-burn only, no delegation today) | `app/custom/aml/` |

## Pattern Overview

**Overall:** Flask blueprint API + Celery background workers + on-chain tronpy client. Background work (sweep, undelegate, balance scan) is celery-driven on a 60-min cadence; HTTP API handles synchronous merchant operations.

**Key characteristics:**
- Two distinct flows both called "payout":
  - **Merchant payout** (HTTP-driven, source = fee_deposit hot wallet) — does NOT need energy delegation.
  - **Sweep / drain** (celery-driven, source = onetime user-wallet) — IS where energy delegation happens. **This is the re:Fee target.**
- Energy delegation is JIT (per-sweep), not pre-staked-pool depletion (though it draws from a pre-staked pool of energy belonging to the `energy_delegator` account).
- Single `Settings` class holds all config; pydantic validates at process startup.

## Data Flow — Sweep (the re:Fee target)

1. Periodic celery task `scan_accounts` (`app/tasks.py:553`) runs every `BALANCES_RESCAN_PERIOD` (default 3600s).
2. For each onetime account whose token balance ≥ `min_transfer_threshold`, it queues `transfer_trc20_from(account, symbol)` (`app/tasks.py:703`).
3. `transfer_trc20_from` (`app/tasks.py:88`):
   - Reads `EnergyLimit` of onetime; skips delegation if already sufficient.
   - Calls `get_estimated_energy(...)` to know exact energy needed (~65k for activated USDT-experienced address).
   - **`tasks.py:354`: `delegate_energy(sun_needed)` — TARGET POINT FOR re:Fee REPLACEMENT.**
   - On success: builds + signs + broadcasts the USDT-TRC20 `transfer(main, balance)` from onetime.
   - On success: schedules `undelegate_energy.delay(onetime)` to reclaim energy.
4. Result reported back to shkeeper main via callback.

## Data Flow — Merchant Payout (NOT re:Fee's concern)

1. shkeeper main → `POST /<symbol>/payout/<dest>/<amount>`
2. Handler at `app/api/payout.py:75` enqueues celery `prepare_payout` → `payout`.
3. `Wallet.transfer(dst, amount)` sends from fee_deposit (pre-funded with TRX/energy).

## Key Abstractions (current + planned)

- **`Wallet`** (`app/wallet.py`) — per-symbol abstraction; default source = fee_deposit.
- **`ConnectionManager`** — tronpy client lifecycle.
- **`KeyType` enum** — `fee_deposit`, `onetime`, `energy` (added by `resource-delegation-mode` PR).
- **`EnergyProvider` (NEW, Phase 1)** — abstraction over energy acquisition. Two implementations: `StakingEnergyProvider` (current freeze-v2 logic, refactored), `RefeeEnergyProvider` (new in Phase 2).

## Entry Points

| Entry | File | Triggered by |
|---|---|---|
| HTTP server | `run.py` (gunicorn loads `server`) | docker container CMD |
| Celery worker | `celery_worker.py` | separate container in helm chart |
| Block scanner thread | `run.py` (background thread) | started inside HTTP container |

## Architectural Constraints

- **Threading:** Flask request threads + celery worker pool; tronpy is sync. ConnectionManager handles failover.
- **Global state:** `config = Settings()` (singleton at `app/config.py:169`); ConnectionManager (singleton).
- **Long sync calls in celery:** `tx.broadcast().wait()` blocks worker; OK because celery handles concurrency. Polling re:Fee will be similarly blocking.

## Anti-Patterns observed

1. **Inline functions inside long celery tasks.** `transfer_trc20_from` defines `calc_sun_for_energy_delegation` and `delegate_energy` as nested functions (~280-line outer function). The re:Fee refactor (Phase 1) lifts them out.

## Error Handling

- Logger at INFO/WARNING in tasks.py; exceptions propagate up to celery, which records and may retry.
- HTTP errors from external services: caught, logged, return False from the task. Celery typically retries on raised exception.

## Cross-Cutting Concerns

- **Logging:** stdlib `logging` via `app/logging.py`; INFO-level for happy path, WARNING for fallbacks.
- **Validation:** pydantic at config layer; manual checks in business logic.
- **Authentication:** HTTP Basic at the sidecar boundary (`API_USERNAME`/`API_PASSWORD`).

---

*Architecture analysis: 2026-04-30*

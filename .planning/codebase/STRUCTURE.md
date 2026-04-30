# Codebase Structure

**Analysis Date:** 2026-04-30
**Source:** spike 002 recon.

## Directory Layout

```
tron-shkeeper/
├── app/
│   ├── __init__.py             # Flask app factory; blueprint registration
│   ├── api/
│   │   ├── payout.py           # HTTP: payout, multipayout, calc-tx-fee, task status
│   │   ├── staking.py          # HTTP: freeze/unfreeze/delegate/undelegate (admin)
│   │   ├── views.py            # HTTP: balance, status, mkaddr, dump, fee-deposit
│   │   └── metrics.py          # HTTP: Prometheus exporters
│   ├── tasks.py                # Celery tasks; transfer_trc20_from is HERE (sweep flow)
│   ├── wallet.py               # Wallet class; per-symbol balance/transfer
│   ├── utils.py                # get_energy_delegator, has_free_bw, helpers
│   ├── connection_manager.py   # tronpy multi-fullnode pool
│   ├── config.py               # Settings (pydantic-settings) — all env vars
│   ├── schemas.py              # pydantic schemas (TronFullnode, Token, etc.)
│   ├── exceptions.py           # custom exceptions
│   ├── logging.py              # logger configuration
│   ├── models.py               # SQLModel ORM
│   ├── db.py                   # SQLite engine wiring
│   ├── wallet_encryption.py    # remote-fetched encryption password from shkeeper main
│   ├── custom/
│   │   └── aml/                # alternative drain workflow (AML check before sweep)
│   │       ├── classes.py      # AmlWallet (TRX-burn only, no delegation today)
│   │       └── schemas.py
│   └── (NEW Phase 1) energy_provider.py   # to be created
├── data/
│   ├── database.db
│   └── tron.db
├── run.py                      # gunicorn entry; also starts block scanner + best-server-refresh threads
├── celery_worker.py            # celery worker entry (separate container)
├── requirements.txt            # pip deps; no pyproject.toml
├── Dockerfile                  # container image
└── .planning/                  # (this) GSD project planning
```

## Directory Purposes

**`app/api/`:** Flask blueprints. Each file owns a slice of the HTTP surface. URLs are symbol-prefixed via Flask URL converter.

**`app/tasks.py`:** Celery tasks. The two largest functions live here: `transfer_trc20_from` (sweep, ~280 lines) and `scan_accounts` (periodic trigger). **The re:Fee integration touches this file at `:354` (and ~6 more lines for fallback/release).**

**`app/custom/aml/`:** Alternative drain pipeline that runs AML check before sweep. Currently TRX-burn only; out of scope for v1 of re:Fee integration unless explicitly extended.

## Key File Locations

**Entry Points:**
- `run.py` — HTTP server entrypoint (gunicorn).
- `celery_worker.py` — celery worker entrypoint.

**Configuration:**
- `app/config.py:Settings` — single source for env vars.

**Core re:Fee Integration:**
- `app/tasks.py:88-418` — `transfer_trc20_from` (the sweep function).
- `app/tasks.py:354` — exact line of the delegation call (TARGET).
- `app/tasks.py:412-416` — post-transfer `undelegate_energy.delay(...)` call.
- `app/tasks.py:421-467` — `undelegate_energy` task (no-op for re:Fee).
- `app/utils.py:100` — `get_energy_delegator()` (called from staking provider, not re:Fee).
- `app/api/staking.py:212` — manual delegate endpoint (admin-facing; not part of automated sweep).

**Tests:**
- None in source (verified: no `tests/` dir, no `test_*.py` files, no pytest config).

## Naming Conventions

- **Files:** snake_case `.py`.
- **Functions:** snake_case (e.g. `transfer_trc20_from`).
- **Classes:** PascalCase (e.g. `Wallet`, `ConnectionManager`, `Settings`).
- **Enums / constants:** UPPER_SNAKE_CASE (e.g. `KeyType.fee_deposit`, env vars `ENERGY_DELEGATION_MODE`).
- **Modules / packages:** snake_case under `app/`.

## Where to Add New Code

**`EnergyProvider` abstraction (Phase 1):**
- New file: `app/energy_provider.py`.
- Definitions: `EnergyProvider` (ABC), `StakingEnergyProvider` (current logic), `RefeeEnergyProvider` (Phase 2), `get_energy_provider()` factory.

**New env vars:**
- Add to `app/config.py:Settings` class. Use existing pattern (typed pydantic field with default).

**Modifications to sweep flow:**
- `app/tasks.py:354` — replace `delegate_energy(sun_needed)` with `provider.acquire(...)`.
- `app/tasks.py:412-416` — guard `undelegate_energy.delay(...)` call to staking source only, or call `provider.release(...)` (no-op for refee).
- Remove inline `calc_sun_for_energy_delegation` (lines 115-120) and `delegate_energy` (lines 122-184). Both lifted into `StakingEnergyProvider`.

**Tests:**
- No tests today. v1 ships without; v2 (later) may add pytest scaffolding under `tests/` (see `shkeeper.io/.planning/codebase/TESTING.md` for the recommended structure).

## Special Directories

**`data/`:** SQLite DBs (`database.db`, `tron.db`); persistent local state for the sidecar.

**`.planning/`:** GSD project planning. Tracked in git per `commit_docs: true` in `config.json`.

---

*Structure analysis: 2026-04-30*

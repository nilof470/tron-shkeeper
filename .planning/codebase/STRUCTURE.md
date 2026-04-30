# Codebase Structure

**Analysis Date:** 2026-04-30

## Directory Layout

```
tron-shkeeper/
├── Dockerfile                       # python:3.13 base, installs requirements.txt, copies repo to /app
├── requirements.txt                 # Pinned runtime deps (Flask 3.1, Celery 5.4, tronpy 0.5, sqlmodel 0.0.22, etc.)
├── run.py                           # gunicorn entry: starts encryption, server-refresh, BlockScanner threads
├── celery_worker.py                 # `celery -A celery_worker.celery worker` entry; pushes Flask app context
├── .dockerignore                    # excludes data/, .env, .venv/, *.db, etc. from image build
├── .gitignore                       # excludes data/, .env, *.db, .venv/, .idea/, *.swp
├── .github/
│   └── workflows/
│       ├── ci.yml                   # main branch CI / image build
│       ├── dev-ci.yml               # dev branch CI / image build
│       └── issue_create_auto_reply.yml
├── .planning/                       # GSD planning artefacts (this folder)
│   └── codebase/
│       ├── ARCHITECTURE.md
│       └── STRUCTURE.md
└── app/                             # Python package, single Flask + Celery app
    ├── __init__.py                  # `create_app()` factory + `celery` instance
    ├── api/                         # Flask blueprints (HTTP layer)
    │   ├── __init__.py              # Defines `api`, `metrics_blueprint`, `staking_bp`; basic-auth `before_request`
    │   ├── views.py                 # /<symbol>/generate-address, /balance, /status, /transaction/<txid>, /addresses, /multiserver/*
    │   ├── payout.py                # /<symbol>/payout, /multipayout, /calc-tx-fee, /task/<id>
    │   ├── staking.py               # /staking/* (freeze/unfreeze/delegate/...)
    │   └── metrics.py               # /metrics (Prometheus exposition)
    ├── block_scanner.py             # `BlockScanner` class + `parse_tx` + `block_scanner_stats`
    ├── config.py                    # `Settings(BaseSettings)` + module-level `config` instance + Token registry
    ├── connection_manager.py        # `ConnectionManager` singleton with multi-node failover
    ├── custom/                      # Optional, config-activated workflows
    │   ├── __init__.py              # empty marker
    │   └── aml/                     # AML / external drain workflow (off unless EXTERNAL_DRAIN_CONFIG set)
    │       ├── __init__.py
    │       ├── classes.py           # `AmlWallet(Wallet)` payout pipeline
    │       ├── functions.py         # AMLBot HTTP, payout-list builder, transaction recorder
    │       ├── models.py            # SQLModel: tron_aml_transactions, tron_aml_payouts
    │       ├── schemas.py           # Pydantic ExternalDrain config (regular_split + aml_check)
    │       └── tasks.py             # Celery: check_transaction, recheck_transaction(s), run_payout_for_tx, sweep_accounts
    ├── db.py                        # SQLite raw helpers + SQLModel `engine`; `init_app()` creates schemas
    ├── exceptions.py                # Project-specific exception classes
    ├── logging.py                   # Configures the project-wide `logger`
    ├── models.py                    # SQLModel tables: tron_settings, tron_keys, tron_balances
    ├── schema.sql                   # Raw DDL for legacy `keys` + `settings` tables (executed by init_db)
    ├── schemas.py                   # Pydantic enums + types: KeyType, TronNetwork, TronSymbol, TronAddress, TronTransaction, Token, SrVote
    ├── tasks.py                     # Top-level Celery tasks (transfer_*, payout, scan_accounts, vote_for_sr, ...)
    ├── trc20balances.sql            # Raw DDL for `trc20balances` (separate SQLite DB)
    ├── utils.py                     # Key helpers, energy/bandwidth math, `skip_if_running`, `DecimalConverter`
    ├── wallet.py                    # `Wallet` class wrapping tronpy balance/transfer
    └── wallet_encryption.py         # `wallet_encryption` namespace class (Fernet + PBKDF2)
```

`data/` is created at runtime (it lives outside the image — the Dockerfile copies the repo without `data/`, and both `.dockerignore` and `.gitignore` list it). It hosts the three SQLite databases:

```
data/
├── database.db        # config.DATABASE  — raw `keys`, `settings`
├── tron.db            # config.DB_URI    — SQLModel-managed tables
└── trc20balances.db   # config.BALANCES_DATABASE — `trc20balances`
```

## Directory Purposes

**`/` (repo root):**
- Purpose: process entry points, container/build manifests.
- Contains: `run.py`, `celery_worker.py`, `Dockerfile`, `requirements.txt`, `.dockerignore`, `.gitignore`.
- Key files: `run.py` (gunicorn target), `celery_worker.py` (Celery target).
- Constraint: keep this directory free of business logic — both entry files exist solely to bootstrap `app/`.

**`app/`:**
- Purpose: the entire Python package, Flask + Celery code, blockchain logic.
- Contains: app factory, configuration, all domain modules, blueprints, persistence helpers, custom workflows.
- Key files: `__init__.py`, `block_scanner.py`, `tasks.py`, `wallet.py`, `connection_manager.py`, `config.py`.

**`app/api/`:**
- Purpose: Flask blueprint package — every HTTP-facing handler lives here.
- Contains: `views.py`, `payout.py`, `staking.py`, `metrics.py`, plus the blueprint declarations in `__init__.py`.
- Constraint: blueprints are wired into the app inside `create_app()` (`app/__init__.py:70-80`); a new blueprint is not registered automatically by being placed in this folder.

**`app/custom/`:**
- Purpose: opt-in workflows enabled by configuration only.
- Contains: `aml/` (the only current member). Use this directory for any future custom drain / compliance workflow rather than threading it through the default blueprints.
- Constraint: anything under `app/custom/` must be import-safe regardless of whether the feature is enabled — `app/db.py:10` unconditionally imports `app/custom/aml/models.py` so SQLModel sees the AML tables at boot.

**`app/custom/aml/`:**
- Purpose: AMLBot integration + external split-payout pipeline gated by `config.EXTERNAL_DRAIN_CONFIG`.
- Contains: domain class `AmlWallet`, helper functions for AML scoring/payout, SQLModel tables, Pydantic config schemas, Celery tasks.

**`.github/workflows/`:**
- Purpose: GitHub Actions pipelines for CI on `main` and `dev` branches plus issue auto-reply.
- Generated: No.
- Committed: Yes.

**`.planning/codebase/`:**
- Purpose: GSD codebase maps consumed by `/gsd-plan-phase` and `/gsd-execute-phase`.
- Generated: Yes (by `/gsd-map-codebase`).
- Committed: Project-dependent.

**`data/` (runtime only):**
- Purpose: writable SQLite + Celery beat schedule storage.
- Generated: Yes, at first run.
- Committed: No (gitignored).

## Key File Locations

**Entry Points:**
- `run.py`: gunicorn target; sets up encryption + ConnectionManager + BlockScanner threads, exposes `server`.
- `celery_worker.py`: Celery target; configures encryption and Flask app context, exposes `celery` (re-export of `app.celery`).

**Configuration:**
- `app/config.py`: `Settings` Pydantic model; module-level `config` is the global handle. `.env` is auto-loaded (`SettingsConfigDict(env_file=".env", extra="ignore")`).
- `requirements.txt`: pinned runtime dependencies.
- `Dockerfile`: container build steps.

**Core Logic:**
- `app/__init__.py`: app factory, Celery instance, blueprint registration, `WATCHED_ACCOUNTS` initialisation.
- `app/block_scanner.py`: block ingestion loop + `parse_tx`.
- `app/tasks.py`: top-level Celery tasks (sweeps, payouts, scans, voting).
- `app/wallet.py`: `Wallet` class for balance/transfer operations.
- `app/connection_manager.py`: Tron RPC client + multi-server fail-over.
- `app/utils.py`: key management, energy/bandwidth math, `skip_if_running` decorator.
- `app/wallet_encryption.py`: encryption gate (must run before any private-key access).

**Persistence:**
- `app/db.py`: SQLite/SQLModel bootstrap (`get_db`, `query_db`, `query_db2`, `engine`, `init_app`).
- `app/models.py`: SQLModel tables (`tron_settings`, `tron_keys`, `tron_balances`).
- `app/schema.sql`: raw DDL for legacy `keys`/`settings`.
- `app/trc20balances.sql`: raw DDL for `trc20balances`.
- `app/custom/aml/models.py`: SQLModel tables (`tron_aml_transactions`, `tron_aml_payouts`).

**HTTP surface:**
- `app/api/__init__.py`: blueprint declarations + auth.
- `app/api/views.py`, `payout.py`, `staking.py`, `metrics.py`: route handlers.

**Schemas / Types:**
- `app/schemas.py`: `KeyType`, `TronNetwork`, `TronSymbol`, `TronAddress`, `TronTransaction`, `Token`, `SrVote`.
- `app/custom/aml/schemas.py`: `ExternalDrain`, `AmlSplitConfig`, `RegularSplitConfig`, `AmlRiskConfig`, `AmlCryptoConfig`.
- `app/exceptions.py`: project exception classes.

**Operational:**
- `app/logging.py`: configures the shared `logger`.
- `app/api/metrics.py`: Prometheus metrics + GitHub release polling.

## Naming Conventions

**Files:**
- Module names: `snake_case.py`, all lowercase, single noun or noun phrase (`block_scanner.py`, `connection_manager.py`, `wallet_encryption.py`).
- Test files: not present — there is no test suite in the repo (`find . -name "*test*" -path './app*'` returns nothing). Place new tests in a top-level `tests/` directory if added.
- SQL DDL: `<table_or_db>.sql` colocated with `app/` (`schema.sql`, `trc20balances.sql`).

**Directories:**
- All package directories use lowercase, no separators (`app`, `api`, `custom`, `aml`).
- Custom workflow modules nest under `app/custom/<feature>/` and mirror the top-level layout (`models.py`, `schemas.py`, `tasks.py`, `functions.py`, `classes.py`).

**Classes:**
- `PascalCase`, descriptive nouns: `BlockScanner`, `ConnectionManager`, `Wallet`, `AmlWallet`, `Settings`, `Setting`, `Key`, `Balance`, `Transaction`, `Payout`, `TronTransaction`, `TronFullnode`, `Token`, `SrVote`.
- Namespace-style classes (no instances) use `lower_snake_case` exceptionally: `wallet_encryption` (`app/wallet_encryption.py:27`). Treat this as a codified pattern only for already-existing classes; new classes should be `PascalCase`.

**Enums:**
- `PascalCase` enum names with `lower_snake_case` members for keys (`KeyType.fee_deposit`, `KeyType.onetime`, `KeyType.energy`, `TronNetwork.mainnet`, `TronNetwork.testnet`).
- `TronSymbol` members are upper-case ticker codes (`TRX`, `USDT`, `USDC`).

**Functions:**
- `lower_snake_case`. Imperative verbs (`add_key`, `get_key`, `parse_tx`, `notify_shkeeper`, `prepare_payout`).
- Celery tasks live at module top-level decorated with `@celery.task(...)` and follow the same naming.

**Constants:**
- `UPPER_SNAKE_CASE` for class-level state (`BlockScanner.WATCHED_ACCOUNTS`, `Wallet.CACHE`, `ConnectionManager.instance`) and config field names (`TRON_NETWORK`, `BLOCK_SCANNER_INTERVAL_TIME`).

**SQLModel tables:**
- Class names in `PascalCase`; `__tablename__` always prefixed `tron_` for default tables and `tron_aml_` for the AML module (`tron_settings`, `tron_keys`, `tron_balances`, `tron_aml_transactions`, `tron_aml_payouts`).
- Note: legacy raw tables (`keys`, `settings`) are unprefixed because they predate the SQLModel layer; do not add new raw tables.

**URL paths:**
- Symbol-scoped routes start with `/<symbol>/...` and the converter pulls `g.symbol = values.pop("symbol").upper()` (`app/api/__init__.py:31-33`). Always use the `decimal:` converter for amounts (registered at `app/__init__.py:68`).

## Where to Add New Code

**New REST endpoint (symbol-scoped, default workflow):**
- Implementation: add a function decorated with `@api.<method>("/route")` in `app/api/views.py` or a new file `app/api/<feature>.py`.
- If you create a new file, import it from `app/api/__init__.py` (see the trailing `from . import payout, views, metrics, staking` at line 44) so the routes register on the existing `api` blueprint.
- Auth: nothing extra — `before_request` in `app/api/__init__.py:13-23` already enforces basic auth on `api`, `metrics_blueprint`, and `staking_bp`.
- URL converter for amounts: declare `<decimal:amount>` (registered at `app/__init__.py:68`).
- Tests: none exist in the repo — see "Testing" gap.

**New REST endpoint (staking / admin):**
- Add the handler to `app/api/staking.py` decorated with `@staking_bp.<method>("/route")`. It will be served under `/staking/...`.

**New Prometheus metric:**
- Declare the `prometheus_client` collector at module level in `app/api/metrics.py`, then update inside `get_metrics` (`app/api/metrics.py:41-58`). Default GC/PLATFORM/PROCESS collectors are already unregistered there.

**New Celery task (default flow):**
- Add to `app/tasks.py` with `@celery.task()` (or `@celery.task(bind=True)` if you need `self`).
- For tasks that should not run concurrently with themselves, wrap with `@skip_if_running` from `app/utils.py:133-152` (after `@celery.task(bind=True)` — order matters; see `app/tasks.py:553-555`).
- For periodic schedules, register inside `setup_periodic_tasks` (`app/tasks.py:804-817`); guard with a `config.<FEATURE>` flag so the schedule remains opt-in.

**New Celery task (AML / custom workflow):**
- Add to `app/custom/aml/tasks.py` and import lazily where needed (see `app/block_scanner.py:184` for the pattern). Do not import AML tasks at module top level outside `app/custom/aml/` — that would couple the default flow to the AML feature.

**New SQLModel table (default):**
- Define in `app/models.py`, prefix `__tablename__` with `tron_`, set `table=True`, and add timestamp columns following `Setting` / `Key` / `Balance` (`app/models.py:11-47`).
- The table will auto-create at boot via `SQLModel.metadata.create_all(engine)` (`app/__init__.py:82-84`); no migration is required for first-time creation but there is no Alembic migration script for column changes — coordinate manually.

**New SQLModel table (AML):**
- Define in `app/custom/aml/models.py`, prefix `__tablename__` with `tron_aml_`. The module is unconditionally imported by `app/db.py:10` so it will be picked up by `metadata.create_all`.

**New configuration option:**
- Add a typed field on `Settings` in `app/config.py` with a sensible default. Keep secrets as `str` and route through `wallet_encryption` if they protect on-chain assets.
- Custom-workflow configs go through Pydantic models in `app/custom/aml/schemas.py` style (or a new `app/custom/<feature>/schemas.py`) and are referenced by `Settings`.

**New on-chain transfer abstraction:**
- Subclass `Wallet` in `app/wallet.py` (or, for opt-in flows, in `app/custom/<feature>/classes.py` mirroring `AmlWallet`). Reuse `Wallet.transfer` for the actual broadcast unless you need bespoke fee handling.

**New blockchain-watch logic:**
- Modify `BlockScanner.scan` (`app/block_scanner.py:179-313`). The current method has two explicit branches (`if config.EXTERNAL_DRAIN_CONFIG: ... else: ...`); add a third branch only via configuration flags, never via runtime imports of unrelated modules.
- Watched accounts must be added through `BlockScanner.add_watched_account` (`app/block_scanner.py:81-86`) so that the in-process set stays in sync.

**New Tron full-node config:**
- Use `MULTISERVER_CONFIG_JSON` env var (parsed as `Json[List[TronFullnode]]` at `app/config.py:51`); single-node setups stay on `FULLNODE_URL` + `TRON_NODE_USERNAME` + `TRON_NODE_PASSWORD`. `ConnectionManager.__init__` (`app/connection_manager.py:34-47`) preferentially uses the JSON list when present.

**New domain exception:**
- Add to `app/exceptions.py`. Subclass `Exception` (or `SystemExit` like `EncryptionModeMismatch`). Catch only at boundaries (`BlockScanner.scan`, request handlers, top of Celery tasks); do not catch broadly inside helpers.

**New shared utility:**
- Place in `app/utils.py` if it touches keys, bandwidth, energy, or Celery dispatch helpers. Otherwise create a new module under `app/` with a single-noun filename.

**New URL converter:**
- Define a class extending `werkzeug.routing.BaseConverter` and register inside `create_app` next to `DecimalConverter` (`app/__init__.py:68`).

**Encryption-aware code:**
- Always route private-key strings through `wallet_encryption.encrypt` / `.decrypt` (`app/wallet_encryption.py:31-37`); never store plaintext private keys in either DB.

## Special Directories

**`data/`:**
- Purpose: hosts the three SQLite databases plus Celery beat scheduler state.
- Generated: Yes (created at runtime; the Docker image does not bake this directory).
- Committed: No (`.gitignore`, `.dockerignore`).
- Persistence: should be mounted as a volume in production — losing it loses every key, every cached balance, and the last-seen-block pointer.

**`.venv/`:**
- Purpose: local virtualenv used during development.
- Generated: Yes.
- Committed: No (gitignored, dockerignored).

**`.idea/`:**
- Purpose: PyCharm project metadata.
- Committed: present in tree but covered by `.gitignore` for new clones; ignore when reading.

**`.planning/`:**
- Purpose: GSD planning artefacts (codebase maps, phase plans).
- Generated: Yes (by GSD commands).
- Committed: project-dependent.

**`.github/`:**
- Purpose: GitHub Actions workflows + issue templates.
- Generated: No.
- Committed: Yes.

---

*Structure analysis: 2026-04-30*

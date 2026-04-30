<!-- refreshed: 2026-04-30 -->
# Architecture

**Analysis Date:** 2026-04-30

## System Overview

```text
┌──────────────────────────────────────────────────────────────────────┐
│                       External Consumers                              │
│   shkeeper-backend (HTTP) │ Prometheus scraper │ Tron full-nodes      │
└────────┬───────────────────┬────────────────────┬───────────────────┘
         │                   │                    ▲
         ▼                   ▼                    │
┌──────────────────────────────────────────────────────────────────────┐
│                       Process: gunicorn (run.py)                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │              Flask app (`app/__init__.py:create_app`)          │  │
│  │  Blueprints: api │ metrics_blueprint │ staking_bp              │  │
│  │  `app/api/views.py` `app/api/payout.py` `app/api/staking.py`   │  │
│  │  `app/api/metrics.py`                                          │  │
│  └────────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────┐  ┌────────────────────────────────────┐   │
│  │ BlockScanner thread  │  │ ConnectionManager refresh thread    │   │
│  │ `app/block_scanner.py│  │ `app/connection_manager.py`         │   │
│  │  :BlockScanner.__call│  │  :refresh_best_server_thread_handler│   │
│  └──────────┬───────────┘  └─────────────┬───────────────────────┘   │
└─────────────┼──────────────────────────────┼─────────────────────────┘
              │                              │
              │ enqueues                     │ chooses best fullnode
              ▼                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                Process: Celery worker (`celery_worker.py`)            │
│  Tasks: `app/tasks.py` (transfer_trc20_from, transfer_trx_from,       │
│         payout, prepare_*payout, scan_accounts, vote_for_sr,          │
│         post_payout_results, undelegate_energy)                       │
│  AML tasks: `app/custom/aml/tasks.py` (check_transaction,             │
│         recheck_transaction(s), run_payout_for_tx, sweep_accounts)    │
└────────────────────────────┬─────────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Persistence + brokers                                                 │
│  SQLite (raw `keys`, `settings`)             `app/db.py:get_db`       │
│  SQLite via SQLModel (tron_settings, tron_keys, tron_balances,        │
│    tron_aml_transactions, tron_aml_payouts) `app/db.py:engine`        │
│  SQLite balances DB `data/trc20balances.db`                           │
│  Redis (broker + result backend)              `app/__init__.py:10-18` │
└──────────────────────────────────────────────────────────────────────┘
              │                              ▲
              │ webhook                      │ HTTP RPC
              ▼                              │
┌─────────────────────────┐    ┌─────────────────────────────────────┐
│ shkeeper backend        │    │ Tron full-nodes (multi-server)       │
│ /api/v1/walletnotify    │    │ HTTPProvider via tronpy              │
│ /api/v1/payoutnotify    │    │ `app/connection_manager.py`          │
│ /api/v1/{symbol}/decrypt│    │                                      │
└─────────────────────────┘    └─────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| `run.py` (gunicorn entry) | Wire encryption, start ConnectionManager refresh thread, build Flask app, start BlockScanner + stats threads | `run.py` |
| `celery_worker.py` (worker entry) | Configure encryption, instantiate Flask app context, expose `celery` for `-A celery_worker.celery` | `celery_worker.py` |
| App factory | Build Flask `app`, register blueprints, init SQLite + SQLModel schemas, populate `BlockScanner.WATCHED_ACCOUNTS`, ensure `fee_deposit` key exists | `app/__init__.py` |
| `BlockScanner` | Pull blocks from current Tron node, parse TRX + TRC20 transfers, notify shkeeper, enqueue Celery transfers, persist last-seen block | `app/block_scanner.py` |
| `ConnectionManager` | Maintain list of full-nodes, persist current server id in `settings`, return cached `tronpy.Tron` clients, periodically pick the highest-block server | `app/connection_manager.py` |
| `Wallet` | High-level TRX / TRC20 balance + transfer abstraction wrapping `tronpy` and main account lookup | `app/wallet.py` |
| `wallet_encryption` | Fetch encryption mode/key from shkeeper, validate against on-disk DB, encrypt/decrypt private keys with Fernet + PBKDF2 | `app/wallet_encryption.py` |
| `app.api` blueprint | REST endpoints under `/<symbol>/*` (address generation, balances, status, transactions, payouts, multiserver) | `app/api/__init__.py`, `app/api/views.py`, `app/api/payout.py` |
| `metrics_blueprint` | Prometheus `/metrics` endpoint exposing scanner + node status | `app/api/metrics.py` |
| `staking_bp` | Staking & resource delegation endpoints under `/staking/*` | `app/api/staking.py` |
| Celery tasks (`app/tasks.py`) | Sweep one-time accounts to fee-deposit, perform payouts, scan balances, SR voting, periodic schedules | `app/tasks.py` |
| AML module | Optional drain/AML workflow: classify deposits, call AMLBot, split payouts | `app/custom/aml/` |
| `db.py` | Two parallel persistence stacks: raw `sqlite3` (`get_db`/`query_db`/`query_db2`) plus SQLModel `engine` for ORM tables | `app/db.py` |
| `models.py` | SQLModel tables `Setting`, `Key`, `Balance` (prefixed `tron_*`) | `app/models.py` |
| `schemas.py` | Pydantic types: `KeyType`, `TronNetwork`, `TronSymbol`, `TronAddress`, `TronTransaction`, `Token`, `SrVote` | `app/schemas.py` |
| `config.py` | `pydantic-settings` `Settings` loaded from env / `.env`, token registry, AML config validation | `app/config.py` |
| `utils.py` | Key management helpers (`add_key`, `get_key`, `get_energy_delegator`), bandwidth / energy estimation, `skip_if_running` Celery decorator, `DecimalConverter` URL converter | `app/utils.py` |

## Pattern Overview

**Overall:** Multi-process Flask + Celery payment gateway with embedded blockchain poller.

**Key Characteristics:**
- Single-codebase, two run modes: gunicorn (`run.py`) and Celery worker (`celery_worker.py`); both share the Flask app context and configuration via `app/__init__.py:create_app`.
- The web process owns the long-running `BlockScanner` (in a daemon thread) — no separate scanner process exists; HTTP requests and on-chain ingestion run in the same Python process.
- Celery (`broker=redis`) is used for fan-out work: TRC20/TRX sweeps from one-time accounts to `fee_deposit`, payouts, periodic balance scans, AML rechecks, and SR voting. The serializer is configured to `pickle` for tasks and results in `app/__init__.py:14-18`; this is a deliberate choice to ship Pydantic / Decimal payloads, and the broker is therefore expected to be on a trusted private network.
- Tron RPC access is funneled through a global singleton `ConnectionManager` (`app/connection_manager.py:17-32`) that keeps a `current_server_id` row in SQLite and refreshes the best server in a background thread.
- Two SQLite databases on local disk: the original raw-SQL DB (`config.DATABASE`, default `data/database.db`) holding `keys` + `settings`, and an SQLModel-managed DB (`config.DB_URI`, default `data/tron.db`) holding `tron_settings`, `tron_keys`, `tron_balances`, `tron_aml_transactions`, `tron_aml_payouts`.
- Custom workflows live under `app/custom/` (currently only `aml/`) and are activated entirely by configuration (`EXTERNAL_DRAIN_CONFIG`); the default scanner branch and the AML branch are explicit `if config.EXTERNAL_DRAIN_CONFIG:` / `else:` paths inside `BlockScanner.scan` (`app/block_scanner.py:223-304`).
- Private keys are encrypted at rest with Fernet using a PBKDF2-derived key fetched from the upstream shkeeper service (`app/wallet_encryption.py`); both `run.py` and `celery_worker.py` block on `setup_encryption()` before doing anything else.

## Layers

**Entry / orchestration layer (`run.py`, `celery_worker.py`):**
- Purpose: bootstrap the process, wire encryption, start daemon threads, expose `server` (Flask WSGI) or `celery` (Celery app).
- Location: repo root.
- Contains: process startup glue, no business logic.
- Depends on: `app` package.
- Used by: gunicorn (web container), `celery -A celery_worker.celery worker` (worker container).

**Application layer (`app/__init__.py`, `app/api/`):**
- Purpose: build the Flask app, register blueprints, expose REST/Prometheus surface, dispatch Celery tasks.
- Location: `app/`, `app/api/`.
- Contains: Flask blueprints, request handlers, Celery enqueues, basic auth handling.
- Depends on: domain layer (`Wallet`, `BlockScanner`, `ConnectionManager`), Celery `tasks` module.
- Used by: external HTTP clients (shkeeper backend, Prometheus).

**Domain / service layer (`app/wallet.py`, `app/block_scanner.py`, `app/connection_manager.py`, `app/utils.py`, `app/wallet_encryption.py`, `app/tasks.py`, `app/custom/aml/classes.py`, `app/custom/aml/functions.py`):**
- Purpose: blockchain interaction, scanning, payouts, key management, AML logic.
- Contains: the core algorithms (block scan loop, transfer pipeline, energy delegation, AML scoring + payout split).
- Depends on: persistence layer + `tronpy` SDK + Redis/Celery.
- Used by: API blueprints, Celery tasks, threads from `run.py`.

**Persistence + config layer (`app/db.py`, `app/models.py`, `app/schemas.py`, `app/exceptions.py`, `app/config.py`, `app/schema.sql`, `app/trc20balances.sql`, `app/custom/aml/models.py`, `app/custom/aml/schemas.py`):**
- Purpose: declare data shapes, open SQLite + SQLModel sessions, hold Pydantic configuration model.
- Contains: SQLModel tables (`tron_*`, `tron_aml_*`), raw-SQL DDL, Pydantic enums/types.
- Depends on: nothing inside the project.
- Used by: every other layer.

## Data Flow

### Primary request path: incoming on-chain deposit

1. `BlockScanner.__call__` loop in the gunicorn process polls the Tron full-node via `ConnectionManager.client()` and computes a chunk of block numbers (`app/block_scanner.py:31-69`, `134-145`).
2. For each block, `BlockScanner.scan` downloads the block + tx-info, runs `parse_tx` to enumerate TRC20/TRX transfers, and filters by `BlockScanner.WATCHED_ACCOUNTS` (`app/block_scanner.py:179-313`, `parse_tx` at `315-397`).
3. **Default branch (no `EXTERNAL_DRAIN_CONFIG`):** if `dst_addr` is a watched one-time account, `notify_shkeeper` POSTs `http://{SHKEEPER_HOST}/api/v1/walletnotify/{symbol}/{txid}` with header `X-Shkeeper-Backend-Key`, then enqueues `transfer_trc20_from.delay(...)` or `transfer_trx_from.delay(...)` (`app/block_scanner.py:271-304`, webhook in `notify_shkeeper` at `167-177`).
4. **AML branch (`EXTERNAL_DRAIN_CONFIG` set):** the same `notify_shkeeper` webhook fires, then `add_transaction_to_db` records a row in `tron_aml_transactions` and `run_payout_for_tx.apply_async(..., countdown=AML_WAIT_BEFORE_API_CALL)` is enqueued (`app/block_scanner.py:223-270`, `app/custom/aml/functions.py:21-63`).
5. Last-seen block is committed to the legacy `settings` table only when every block in the chunk returns `True` (`app/block_scanner.py:51-61`, `set_last_seen_block_num` at `119-127`).
6. The Celery worker picks up `transfer_trc20_from` / `transfer_trx_from`, optionally delegates energy from `fee_deposit`, and moves funds to the main account (`app/tasks.py:88-419`, `421-467`, `470-519`).
7. After payouts, `post_payout_results` POSTs `http://{SHKEEPER_HOST}/api/v1/payoutnotify/{symbol}` with the JSON results (`app/tasks.py:522-533`).

### Outbound payout request path

1. shkeeper backend POSTs `/<symbol>/multipayout` (or `/<symbol>/payout/<to>/<amount>`) with HTTP basic auth (`app/api/__init__.py:13-23`, `app/api/payout.py:22-92`).
2. The view validates input, balances, and TRX fee budget, then chains `prepare_multipayout.s(...) | payout.s(...)` and returns the Celery task id (`app/api/payout.py:72-83`).
3. The chain runs in the worker: `prepare_multipayout` produces a list of `{dst, amount}` dicts, `payout` executes `Wallet.transfer` per item via `ThreadPoolExecutor(max_workers=CONCURRENT_MAX_WORKERS)`, and finally `post_payout_results.delay` notifies shkeeper (`app/tasks.py:75-86`).
4. Clients poll status via `POST /<symbol>/task/<id>` which reads `celery.AsyncResult` (`app/api/payout.py:86-92`).

### One-time address generation

1. shkeeper POSTs `/<symbol>/generate-address` (`app/api/views.py:20-41`).
2. `tronpy.Tron().generate_address()` produces a key pair; the private key is encrypted via `wallet_encryption.encrypt` and inserted into `keys` with `type='onetime'`.
3. `BlockScanner.add_watched_account` updates the in-process `WATCHED_ACCOUNTS` set so the next block scan picks up incoming transfers.

### Connection-manager refresh

1. On startup, `run.py` spawns a daemon thread targeting `ConnectionManager.manager().refresh_best_server_thread_handler` (`run.py:16-21`, `app/connection_manager.py:157-178`).
2. The thread chooses the server with the highest block height and writes it back to `settings.current_server_id`; subsequent `ConnectionManager.client()` calls read that value (`app/connection_manager.py:49-80`, `137-156`).

**State Management:**
- Watched accounts: class-level `BlockScanner.WATCHED_ACCOUNTS` set, populated at app-create and mutated by `/generate-address` (`app/block_scanner.py:29`, `app/__init__.py:50-66`).
- Last-seen block + current server id: rows in raw `settings` table (`app/schema.sql`, `app/block_scanner.py:98-127`, `app/connection_manager.py:65-80`).
- Wallet encryption status/key: class attributes `wallet_encryption.encryption` / `.key` (`app/wallet_encryption.py:27-30`).
- Wallet contract caches: `Wallet.CACHE` dict (`app/wallet.py:14-18`).

## Key Abstractions

**`BlockScanner` (`app/block_scanner.py:28-313`):**
- Purpose: callable object that loops indefinitely, downloading and parsing Tron blocks for watched accounts.
- Pattern: singleton-by-convention (instantiated once in `run.py`); state held on the class via `WATCHED_ACCOUNTS` so other modules can read it without an instance.

**`ConnectionManager` (`app/connection_manager.py:17-178`):**
- Purpose: per-process singleton wrapping `tronpy.Tron` clients with multi-server fail-over.
- Pattern: classmethod-based singleton (`get_instance` / `client` / `manager`); per-call construction of `tronpy.Tron` with shared HTTP adapter.

**`Wallet` (`app/wallet.py:14-115`):**
- Purpose: thin OO wrapper around TRX / TRC20 transfer + balance lookup, parameterised by `symbol`.
- Pattern: per-call instance; `main_account` is a class attribute resolved from the DB at import time. `AmlWallet` (`app/custom/aml/classes.py:14`) extends it for the AML payout flow.

**`wallet_encryption` (`app/wallet_encryption.py:27-188`):**
- Purpose: process-wide encryption gate; classmethods only, no instances.
- Pattern: namespace class; both web and worker call `setup_encryption()` first thing.

**`Settings` (`app/config.py:13-167`):**
- Purpose: typed configuration pulled from environment + `.env` via `pydantic-settings`; module-level `config` is the canonical source.
- Pattern: cached lookups (`@cache`) for token metadata; explicit `__hash__` so the cache works on the Settings instance.

**Pydantic / SQLModel data types (`app/schemas.py`, `app/models.py`, `app/custom/aml/schemas.py`, `app/custom/aml/models.py`):**
- Purpose: validate Tron addresses (`TronAddress` annotated type), enforce token symbol enums, declare DB tables.
- Pattern: SQLModel tables prefixed `tron_` / `tron_aml_`; Pydantic enums used freely as type hints and as URL-derived values (`g.symbol` upper-cased before binding).

## Entry Points

**Flask web app (gunicorn):**
- Location: `run.py` (module-level `server = app.create_app()` exposed for `gunicorn run:server`).
- Triggers: HTTP requests + the daemon threads it starts (BlockScanner + best-server refresh + scanner stats).
- Responsibilities: handle REST + Prometheus traffic, ingest blocks, dispatch Celery tasks.

**Celery worker:**
- Location: `celery_worker.py` (`celery -A celery_worker.celery worker`).
- Triggers: Redis broker messages, Celery beat schedule registered in `app/tasks.py:setup_periodic_tasks` (`scan_accounts`, `vote_for_sr`, AML periodic tasks).
- Responsibilities: TRC20/TRX sweeps to main account, payouts, AML checks, SR voting.

**Background daemon threads (started inside the gunicorn process by `run.py`):**
- `Refresh best server` → `ConnectionManager.refresh_best_server_thread_handler` (`run.py:16-21`).
- `Block Scanner` → `BlockScanner.__call__` (`run.py:33-40`).
- `Scanner Stats` → `block_scanner_stats` (`run.py:42-48`).

## Architectural Constraints

- **Threading:** The web process is a multi-threaded Python program — gunicorn workers plus three daemon threads from `run.py` (`Refresh best server`, `Block Scanner`, `Scanner Stats`). Inside `BlockScanner.__call__` a `ThreadPoolExecutor(max_workers=BLOCK_SCANNER_MAX_BLOCK_CHUNK_SIZE)` parallelises block scans; the default is 1, so beware before raising it because `download_block` and `download_tx_info_by_block_num` use `functools.lru_cache(maxsize=BLOCK_SCANNER_MAX_BLOCK_CHUNK_SIZE)` on instance methods (`app/block_scanner.py:147-165`).
- **Process model:** Celery is forked by default. SQLAlchemy is configured with `poolclass=NullPool` in `app/db.py` precisely because pooled connections do not survive Celery's fork (`app/db.py:12-32`). Do not change to a pooled engine without rerouting Celery to `--pool=solo`.
- **Global mutable state:**
  - `BlockScanner.WATCHED_ACCOUNTS` (class-level `set`) is mutated from any thread that calls `add_watched_account` (`app/block_scanner.py:29`, `81-86`). Access is unsynchronised — assume Python set thread-safety only for single ops.
  - `wallet_encryption.encryption` / `.key` (`app/wallet_encryption.py:27-30`).
  - `ConnectionManager.instance` singleton (`app/connection_manager.py:18-23`).
  - `Wallet.CACHE` dict (`app/wallet.py:14-18`) and `Wallet.main_account` class attribute resolved at import (`app/wallet.py:19`).
  - Module-level `config = Settings()` (`app/config.py:169`).
- **Two databases, two access styles:** `app/db.py` exposes both raw `sqlite3` (`get_db` / `query_db` / `query_db2`) talking to `config.DATABASE` and a SQLModel `engine` talking to `config.DB_URI`. Treat the raw `keys` / `settings` tables as the source of truth for keys + scanner state, and the SQLModel `tron_*` tables as the modern home for new persistence.
- **Celery serializer:** `app/__init__.py:14-18` configures `task_serializer="pickle"`, `accept_content=["pickle"]`, `result_serializer="pickle"`. Anything sent over Celery (Pydantic models, Decimals) must round-trip through this serializer; the broker is therefore expected to be on a trusted private network (Redis under `config.REDIS_HOST`). Do not expose Redis to untrusted clients.
- **Pydantic v2 + SQLModel pinning:** versions are pinned in `requirements.txt` (`pydantic==2.10.4`, `sqlmodel==0.0.22`) — when adding tables, validate with the same versions to avoid `Field(sa_column=...)` regressions.
- **Encryption ordering:** Both entry points must call `wallet_encryption.setup_encryption()` before any DB read of private keys (`run.py:10`, `celery_worker.py:1-2`). `_validate_encryption_settings` will `SystemExit` on mismatch — do not catch it.
- **Circular imports:** `BlockScanner.scan` imports `app.tasks` and `app.custom.aml.tasks` lazily inside the method to avoid the circular Celery <-> scanner chain (`app/block_scanner.py:180-184`). Keep new dependencies between `tasks.py`, `block_scanner.py`, and `custom/aml/*` lazy.

## Anti-Patterns

### `query_db2` reopens the SQLite connection per call

**What happens:** `query_db2` (`app/db.py:61-72`) constructs a fresh `sqlite3.connect(config.DATABASE, ...)` on every invocation, sets `journal_mode=wal`, runs the query, and discards the connection. Hot paths (`BlockScanner.set_last_seen_block_num`, `ConnectionManager.get_current_server_id`, `Wallet.main_account` resolution) call it on every block / RPC.
**Why it's wrong here:** Connection setup adds latency on each block scan and creates extra fsyncs in the WAL during the busy scan loop; it also bypasses any future migration to a managed DB driver.
**Do this instead:** New code that runs inside the Flask request lifecycle should use `get_db()` / `query_db()` (`app/db.py:35-58`) which caches the connection on `flask.g`. New persistence work should use SQLModel `Session(engine)` (`app/tasks.py:564`, `app/custom/aml/tasks.py:58-69`).

### Two parallel persistence stacks for the same domain concepts

**What happens:** `keys` / `settings` tables are written through raw SQL while `tron_keys` / `tron_settings` exist as SQLModel tables (`app/models.py:11-33`, `app/schema.sql`). `BlockScanner` and `ConnectionManager` use the raw tables exclusively (`app/block_scanner.py:99-127`, `app/connection_manager.py:65-80`).
**Why it's wrong here:** Anyone adding a setting or key field must remember to touch both schemas, or the data drifts. There is no migration plan documented.
**Do this instead:** Until a migration ships, keep raw tables as the canonical source for `keys` and `settings`; add new persistence to SQLModel only (e.g. `Balance`, `Transaction`, `Payout`). Document any cross-stack writes explicitly.

### `Wallet.main_account` resolved at import time

**What happens:** `app/wallet.py:19` runs `query_db2(...)` as a class-attribute initialiser. This means the module cannot be imported unless the SQLite DB is reachable and `fee_deposit` already exists.
**Why it's wrong here:** It couples module import to runtime state and silently captures the row used by every later instance; rotating the fee-deposit key requires a process restart.
**Do this instead:** When refactoring, move `main_account` to a `functools.cached_property` (already used for `BlockScanner.main_account` at `app/block_scanner.py:92-96`). New `Wallet`-style helpers should resolve keys via `get_key(KeyType.fee_deposit)` (`app/utils.py:66-79`).

### `setup_periodic_tasks` mutates the worker's task graph at import time

**What happens:** `app/tasks.py:804-817` registers periodic tasks based on `config.SR_VOTING` and `config.EXTERNAL_DRAIN_CONFIG` inside Celery's `on_after_configure` signal.
**Why it's wrong here:** Configuration changes require redeploying both the web and worker because the schedule is locked at boot time; there is no way to disable AML rechecks without restarting the worker.
**Do this instead:** New scheduled work should be added through this same hook for consistency, but make the schedule data-driven (e.g. read `config` inside each task body rather than at registration time) so toggling features only requires env reload + process restart of the worker.

## Error Handling

**Strategy:** Coarse `try/except` around each top-level loop iteration plus a Flask-blueprint error handler.

**Patterns:**
- `BlockScanner.__call__` catches `NoServerSet` (sleep 1 s) and bare `Exception` (log + sleep 60 s) (`app/block_scanner.py:62-68`). `BlockScanner.scan` returns `False` on exception so the chunk is retried (`app/block_scanner.py:308-310`).
- `parse_tx` raises domain exceptions (`UnknownTransactionType`, `BadContractResult`, `UnknownToken`) defined in `app/exceptions.py:1-22`; the scanner caller treats them as "skip this tx".
- The `api` blueprint installs `handle_exception` which logs the traceback and returns `{"status": "error", "msg": str(e)}` for any non-`HTTPException` (`app/api/__init__.py:36-41`). `metrics_blueprint` and `staking_bp` do not install one, so unhandled exceptions there bubble up as 500s.
- Celery tasks rely on Celery's retry semantics implicitly; long-running flows (`transfer_trc20_from`) emit explicit `logger.warning(...); return` on guard failures (`app/tasks.py:130-365`).
- `wallet_encryption._validate_encryption_settings` raises `EncryptionModeMismatch(SystemExit)` to abort the process on a mode mismatch (`app/wallet_encryption.py:23-25`, `134-154`).

## Cross-Cutting Concerns

**Logging:** Single root logger configured in `app/logging.py:1-23` — `levelname filename:lineno threadName funcName(): message`. Importing `app.logging` also reformats Flask's default handler. The `DEBUG` level is gated by `config.DEBUG`. Use `from app.logging import logger`; never `print()`.

**Authentication:** HTTP basic auth enforced by `check_credentials` registered as `before_request` on every blueprint (`app/api/__init__.py:13-23`). Credentials come from `config.API_USERNAME` / `config.API_PASSWORD`. Outbound webhooks to shkeeper are authenticated by header `X-Shkeeper-Backend-Key: {SHKEEPER_BACKEND_KEY}` (`app/block_scanner.py:172-175`, `app/tasks.py:526-530`).

**Configuration:** `app.config.config` (Pydantic Settings instance). When inside a Flask request, the same dict is bound on `current_app.config` via the custom `AttrConfig` adapter (`app/__init__.py:26-44`).

**Validation:** Pydantic models (`app/schemas.py`, `app/custom/aml/schemas.py`) and the `TronAddress` annotated type validate addresses everywhere they appear in payloads or settings.

**Metrics:** Prometheus instrumentation lives only in `app/api/metrics.py`; the default GC/PLATFORM/PROCESS collectors are unregistered there. New metrics should be declared module-level in this file.

**Encryption:** Private keys at rest are Fernet-encrypted with PBKDF2 (`SHA-256`, 500 000 iterations, fixed salt `Shkeeper4TheWin!`); never log raw `private` columns. `wallet_encryption.encrypt` is a no-op when shkeeper reports `persistent_status=disabled`.

---

*Architecture analysis: 2026-04-30*

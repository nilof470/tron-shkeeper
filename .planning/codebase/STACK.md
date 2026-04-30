# Technology Stack

**Analysis Date:** 2026-04-30

## Languages

**Primary:**
- Python 3.13 — All application code under `app/` and entry points (`run.py`, `celery_worker.py`)

**Secondary:**
- SQL (SQLite dialect) — Embedded raw schema files: `app/schema.sql`, `app/trc20balances.sql`
- Embedded SQL strings — Raw queries throughout `app/db.py`, `app/connection_manager.py`, `app/api/views.py`, `app/utils.py`, `app/wallet.py`, etc.

## Runtime

**Environment:**
- Python 3.13 (pinned via Dockerfile base image `python:3.13` at `Dockerfile:1`)
- Local development venv present at `.venv/` (gitignored via `.gitignore:1`)

**Package Manager:**
- pip (used inside the Docker image: `pip install --no-cache-dir -r requirements.txt` at `Dockerfile:6`)
- Lockfile: Not present. Only pinned `requirements.txt` (no `requirements.lock`, `Pipfile.lock`, `poetry.lock`, or `pdm.lock`).

## Frameworks

**Core:**
- Flask 3.1.0 — HTTP API web framework. App factory in `app/__init__.py:26-86` (`create_app`). Custom `AttrConfig` config subclass declared at `app/__init__.py:29-39`. Three blueprints registered: `api` (`/<symbol>` prefix), `metrics_blueprint` (`/`), `staking_bp` (`/staking`) defined in `app/api/__init__.py:8-10`.
- Celery 5.4.0 — Distributed task queue, used for payouts, account scanning, AML checks, energy delegation, voting. Instance configured at `app/__init__.py:10-18` with Redis broker/backend and a binary serializer for tasks/results. Worker bootstrap in `celery_worker.py`.
- SQLModel 0.0.22 — ORM for the SQL store, see models in `app/models.py` and `app/custom/aml/models.py`. Engine created with `NullPool` at `app/db.py:12-32` to avoid Celery fork issues.
- SQLAlchemy (transitive via SQLModel) — `NullPool`, `DateTime`, `UniqueConstraint`, `func` imports at `app/db.py:5`, `app/models.py:6`.
- Pydantic 2.10.4 + pydantic-settings 2.7.0 — Strongly-typed environment-driven configuration in `app/config.py:13-167` (`Settings(BaseSettings)`), domain schemas in `app/schemas.py`, AML config schemas in `app/custom/aml/schemas.py`.
- gunicorn 23.0.0 — WSGI HTTP server (declared in `requirements.txt:5`; not invoked from inside the repo Dockerfile, intended to be invoked externally / by deployment manifests).

**Testing:**
- Not detected. No `pytest`, `unittest`, `tests/`, `test_*.py`, or `*_test.py` files were found in the repository.

**Build/Dev:**
- Docker — single-stage `Dockerfile` (4 layers: `FROM python:3.13`, `WORKDIR /app`, `COPY requirements.txt`, `RUN pip install`, `COPY . .`).
- GitHub Actions — release/dev image build pipelines at `.github/workflows/ci.yml` (publishes `vsyshost/tron-shkeeper:{version}` on `v*.*.*` tags) and `.github/workflows/dev-ci.yml` (publishes `vsyshost/tron-shkeeper:dev-{branch}-{sha}` on every push). Uses `docker/metadata-action@v4`, `docker/login-action@v2`, `docker/build-push-action@v4`.
- `.github/workflows/issue_create_auto_reply.yml` — issue auto-reply bot.

## Key Dependencies

**Critical (from `requirements.txt`):**
- `tronpy==0.5.0` — Tron blockchain SDK. Provides `Tron`, `HTTPProvider`, `PrivateKey`, `trx_abi`, `current_timestamp`, exceptions (`AddressNotFound`, `BadKey`, `UnknownError`, `ValidationError`, `TransactionNotFound`). Used in `app/connection_manager.py`, `app/wallet.py`, `app/tasks.py`, `app/utils.py`, `app/block_scanner.py`, `app/wallet_encryption.py`, `app/api/staking.py`, `app/api/views.py`, `app/api/payout.py`, `app/schemas.py`.
- `flask==3.1.0` — Web framework (see Frameworks).
- `celery==5.4.0` — Async task queue (see Frameworks).
- `sqlmodel==0.0.22` — ORM (see Frameworks).
- `pydantic==2.10.4` + `pydantic-settings==2.7.0` — Config + schemas (see Frameworks).
- `redis==5.2.1` — Redis client. Used by Celery as broker and result backend (URL `redis://{REDIS_HOST}` configured at `app/__init__.py:12-13`).
- `cryptography==44.0.0` — Wallet private-key encryption: `Fernet`, `PBKDF2HMAC`, `hashes.SHA256()` in `app/wallet_encryption.py:7-9, 159-165`.
- `requests==2.32.3` — HTTP client for outbound calls to Shkeeper backend, AML provider, GitHub releases, and Tron node. Used in `app/connection_manager.py`, `app/block_scanner.py`, `app/tasks.py`, `app/wallet_encryption.py`, `app/custom/aml/functions.py`, `app/api/metrics.py`, `app/api/views.py`.
- `prometheus-client==0.21.1` — Metrics exposition at `/metrics`. `Gauge`, `Info`, `generate_latest` and unregistration of default collectors in `app/api/metrics.py:1-58`.
- `gunicorn==23.0.0` — Production WSGI server (run command provided externally; the WSGI entry is `run:server` per `run.py:27`).
- `alembic==1.14.0` — DB migration tool listed in `requirements.txt:1`. **No `alembic.ini`, `migrations/`, or `versions/` directory exists in the repo.** Schema is currently bootstrapped via `SQLModel.metadata.create_all(engine)` at `app/__init__.py:84` and raw `schema.sql` / `trc20balances.sql` executed by `app/db.py:75-93`.
- `pymysql==1.1.1` — MySQL driver listed in `requirements.txt:9`. **No active MySQL configuration is wired up:** the only DB URI in code is `sqlite:///data/tron.db` (`app/config.py:19`) and `sqlite3.connect(...)` is used directly (`app/db.py:37, 63, 85`). PyMySQL is shipped to support an alternative `DB_URI` (e.g. `mysql+pymysql://...`) if the deployer overrides `DB_URI` env var.

**Infrastructure (transitive / first-party imports observed):**
- `eth_abi.exceptions` — `NonEmptyPaddingBytes`, `InsufficientDataBytes` used to gracefully handle TRC-20 log decoding errors (`app/block_scanner.py:11`).
- `werkzeug.exceptions` / `werkzeug.routing.BaseConverter` — Flask URL converters (`app/api/__init__.py:3`, `app/utils.py:15, 27-32`).
- Standard library: `sqlite3`, `concurrent.futures.ThreadPoolExecutor`, `threading`, `decimal`, `hashlib`, `base64`, `functools`, `traceback`, `urllib.parse`.

## Configuration

**Environment:**
- All runtime configuration is loaded via `pydantic_settings.BaseSettings` in `app/config.py:13-167`.
- Source: process environment OR `.env` file at the working directory (`SettingsConfigDict(env_file=".env", extra="ignore")` at `app/config.py:14`).
- Singleton instance created at module import time: `config = Settings()` (`app/config.py:169`).
- `.env` is gitignored (`.gitignore:20`) — file existence is expected at deploy time, contents are never committed.

**Key configs required:**
- Tron node connectivity: `FULLNODE_URL`, `TRON_NODE_USERNAME`, `TRON_NODE_PASSWORD`, `TRON_CLIENT_TIMEOUT`, optional `MULTISERVER_CONFIG_JSON`.
- Network selection: `TRON_NETWORK` (`main` | `nile`).
- Storage: `DATABASE` (SQLite path for `keys`/`settings` tables), `DB_URI` (SQLAlchemy/SQLModel URI), `BALANCES_DATABASE`.
- Broker: `REDIS_HOST`.
- API auth: `BTC_USERNAME` (alias for `API_USERNAME`), `BTC_PASSWORD` (alias for `API_PASSWORD`).
- Shkeeper integration: `SHKEEPER_HOST`, `SHKEEPER_BACKEND_KEY`.
- Energy delegation, voting, AML, fee/threshold tunables: see full list in `app/config.py:16-79`.
- Token list: `TOKENS` (default-baked TRC-20 tokens for USDT/USDC on mainnet, JST contract on Nile testnet at `app/config.py:81-103`).

**Build:**
- `Dockerfile` (4 lines of meaningful content) — `python:3.13` base, `WORKDIR /app`, `pip install -r requirements.txt`, `COPY . .`. **No `CMD`/`ENTRYPOINT`** — the run command is provided by the orchestrator (Docker Compose / k8s manifest external to this repo).
- `.dockerignore` (`/Users/test/PycharmProjects/tron-shkeeper/.dockerignore`) excludes venv/data/db/IDE/cache files.

## Platform Requirements

**Development:**
- Python 3.13 interpreter.
- A reachable Redis instance (default `localhost`).
- A reachable Tron full node (default `http://fullnode.tron.shkeeper.io`).
- Disk-writable `data/` directory for SQLite files (`data/database.db`, `data/tron.db`, `data/trc20balances.db`).
- A reachable Shkeeper backend at `SHKEEPER_HOST` for wallet-encryption key fetch (`app/wallet_encryption.py:80-83`) and payout/walletnotify notifications.

**Production:**
- Linux container (Docker image `vsyshost/tron-shkeeper:{version}`), built/pushed by `.github/workflows/ci.yml`.
- Three concurrent processes typically run from the same image:
  1. WSGI server: `gunicorn run:server` (loads `run.py` which spawns the block-scanner and best-server-refresh threads as daemons).
  2. Celery worker: `celery -A celery_worker.celery worker` (entry point `celery_worker.py`).
  3. Celery beat (implicit via `@celery.on_after_configure.connect` periodic task setup at `app/tasks.py:804-817`).
- External services: Redis (broker+backend), Tron full node(s), Shkeeper backend, optional AML provider (AMLBot-style endpoint), optional MySQL if `DB_URI` is overridden.

---

*Stack analysis: 2026-04-30*

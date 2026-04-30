# External Integrations

**Analysis Date:** 2026-04-30

## APIs & External Services

**Tron blockchain (mandatory, primary integration):**
- Service: Tron full node(s) — JSON-RPC / wallet HTTP API.
  - SDK/Client: `tronpy==0.5.0` (`from tronpy import Tron`, `from tronpy.providers import HTTPProvider`).
  - Node URL: `FULLNODE_URL` env var (default `http://fullnode.tron.shkeeper.io`, `app/config.py:26`).
  - Auth: HTTP Basic via `TRON_NODE_USERNAME` / `TRON_NODE_PASSWORD` env vars; the credentials are inlined into the URL netloc by `urlparse` in `app/connection_manager.py:38-43`.
  - Connection timeout: `TRON_CLIENT_TIMEOUT` (default `10s`, `app/config.py:29`), passed into `HTTPProvider(..., timeout=...)` at `app/connection_manager.py:57-58`.
  - Pooling: `requests.adapters.HTTPAdapter(pool_maxsize=100)` mounted onto the tronpy session for both `http://` and `https://` (`app/connection_manager.py:60-62`).
  - Multi-server failover: optional `MULTISERVER_CONFIG_JSON` env var (a JSON list of `TronFullnode{name, url}`) parsed at `app/config.py:51`. The current best server is tracked in the `settings` table and refreshed every `MULTISERVER_REFRESH_BEST_SERVER_PERIOD` seconds (default 20) by `ConnectionManager.refresh_best_server_thread_handler` (`app/connection_manager.py:149-178`).
  - Direct HTTP calls (bypassing tronpy): `wallet/getnodeinfo`, `wallet/getblockbynum`, `wallet/getcandelegatedmaxsize`, `wallet/gettransactioninfobyblocknum`, `wallet/estimateenergy` — issued via `requests.get/post` or `tron_client.provider.make_request(...)` in `app/connection_manager.py:87, 101-104`, `app/tasks.py:124-127, 158`, `app/utils.py:130`, `app/block_scanner.py:157-159`.

**Tron release tracker (informational, optional):**
- Service: GitHub Releases API — `https://api.github.com/repos/tronprotocol/java-tron/releases/latest`.
  - SDK/Client: plain `requests.get(...).json()` (`app/api/metrics.py:23`).
  - Auth: None (anonymous public endpoint).
  - Cached for 24h via `functools.lru_cache(maxsize=2)` keyed on a daily TTL hash (`app/api/metrics.py:17-27`).
  - Surfaced as a Prometheus `Info` metric (`tron_fullnode_last_release`).

**Shkeeper backend (mandatory, parent platform):**
- Service: Shkeeper main app — HTTP API.
  - Host: `SHKEEPER_HOST` env var (default `localhost:5000`, `app/config.py:33`); always called over `http://` (no TLS) in code.
  - Auth: shared header `X-Shkeeper-Backend-Key: {SHKEEPER_BACKEND_KEY}` (default `"shkeeper"`, `app/config.py:32`).
  - Endpoints called outbound:
    - `GET http://{SHKEEPER_HOST}/api/v1/{symbol}/decrypt` — fetches wallet encryption status/key during boot (`app/wallet_encryption.py:80-83`). Polled in a loop via `itertools.cycle` over `["TRX", USDT, USDC, ...]` until a definitive answer is returned (`app/wallet_encryption.py:69-109`).
    - `POST http://{SHKEEPER_HOST}/api/v1/walletnotify/{symbol}/{txid}` — notifies Shkeeper of a new on-chain deposit observed by the block scanner (`app/block_scanner.py:172-177`).
    - `POST http://{SHKEEPER_HOST}/api/v1/payoutnotify/{symbol}` — notifies Shkeeper of completed payout results, retried indefinitely with 10s back-off (`app/tasks.py:524-533`).

**AML provider (optional, AMLBot-style):**
- Service: External AML risk-scoring API (configured per-deployment).
  - SDK/Client: plain `requests.post(...)` (`app/custom/aml/functions.py:101-114, 122-128`).
  - Endpoint base: `EXTERNAL_DRAIN_CONFIG.aml_check.access_point` (e.g. `https://amlbot.example/api/v1`).
  - Auth: signed request token. The MD5 of `"{txid}:{access_key}:{access_id}"` is sent as `token` form field along with `accessId` (`app/custom/aml/functions.py:99-100, 119-121`).
  - Scoring call: `POST {access_point}/` with form fields `hash`, `address`, `asset` (always `"TRX"`), `direction=deposit`, `token`, `accessId`, `locale=en_US`, `flow` (`fast` | `accurate` | `advanced`).
  - Recheck call: `POST {access_point}/recheck` with form body `uid={uid}&accessId={...}&token={...}`.
  - Activated only when `EXTERNAL_DRAIN_CONFIG` env var is set (parsed into `ExternalDrain` schema at `app/custom/aml/schemas.py:88-91`).
  - Periodic Celery beat tasks: `recheck_transactions` every `AML_RESULT_UPDATE_PERIOD` seconds (default 120), `sweep_accounts` every `AML_SWEEP_ACCOUNTS_PERIOD` seconds (default 3600); see `app/tasks.py:809-815`. The first AML scoring call for a transaction is delayed by `AML_WAIT_BEFORE_API_CALL` (default 320s) to let the AML provider ingest the transaction (`app/block_scanner.py:255`).

## Data Storage

**Databases:**

- **SQLite (active, primary store)** — three separate database files, each opened with `journal_mode=wal` and `isolation_level=None` (autocommit):
  - `DATABASE` (default `data/database.db`, `app/config.py:18`) — holds the `keys` and `settings` tables (raw schema in `app/schema.sql`). Used via direct `sqlite3.connect` in `app/db.py:37-44, 63-72` (`get_db`, `query_db`, `query_db2`). The `keys` table stores wallet keypairs (encrypted private keys), and `settings` stores `last_seen_block_num` and `current_server_id`.
  - `DB_URI` (default `sqlite:///data/tron.db`, `app/config.py:19`) — SQLModel/SQLAlchemy engine with `NullPool` (`app/db.py:12-32`). Tables: `tron_settings`, `tron_keys`, `tron_balances` (declared in `app/models.py`); plus `tron_aml_transactions`, `tron_aml_payouts` (declared in `app/custom/aml/models.py`). Created at boot via `SQLModel.metadata.create_all(engine)` (`app/__init__.py:84`).
  - `BALANCES_DATABASE` (default `data/trc20balances.db`, `app/config.py:20`) — holds the legacy `trc20balances` table (raw schema in `app/trc20balances.sql`). Initialized by `init_balances_db` at `app/db.py:83-93`. Note: the active balance store is `tron_balances` in `DB_URI`; the `trc20balances` table is bootstrapped but no read/write code paths target it.

- **MySQL (optional, not currently configured)** — `pymysql==1.1.1` is shipped in `requirements.txt:9` to allow operators to override `DB_URI` to e.g. `mysql+pymysql://user:pass@host/db`. No code references PyMySQL or MySQL directly; it would be activated transparently by SQLAlchemy if `DB_URI` were changed.

- **Alembic migrations** — `alembic==1.14.0` in `requirements.txt:1` but **no `alembic.ini`, `migrations/`, or `versions/` directory present**. All schema is currently created at startup via `SQLModel.metadata.create_all(engine)` (`app/__init__.py:84`) and raw `executescript()` of `app/schema.sql` and `app/trc20balances.sql` in `app/db.py:75-93`.

**File Storage:**
- Local filesystem only. Logs go to `stderr` (`logging.StreamHandler()` in `app/logging.py:15`). No S3/GCS/Azure Blob references.

**Caching:**
- Redis 5.2.1 — used solely as Celery broker and result backend (`redis://{REDIS_HOST}` at `app/__init__.py:12-13`). No standalone caching layer (no `redis.Redis(...)` direct usage in app code).
- In-process caches:
  - `Wallet.CACHE` for tronpy `contract` and `decimals` lookups (`app/wallet.py:15-37`).
  - `functools.cache` decorators on `Settings.get_contract_address`, `Settings.get_min_transfer_threshold`, `Settings.get_symbol`, `Settings.get_tokens` (`app/config.py:105-147`).
  - `functools.lru_cache(maxsize=BLOCK_SCANNER_MAX_BLOCK_CHUNK_SIZE)` on `BlockScanner.download_block` and `BlockScanner.download_tx_info_by_block_num` (`app/block_scanner.py:147-165`).

## Authentication & Identity

**Inbound API auth (Flask):**
- HTTP Basic — enforced for every request to the `api`, `metrics_blueprint`, and `staking_bp` blueprints via the shared `before_request` hook at `app/api/__init__.py:13-23`.
- Credentials: `API_USERNAME` / `API_PASSWORD` (loaded from env vars `BTC_USERNAME` / `BTC_PASSWORD` via Pydantic `Field(alias=...)` in `app/config.py:30-31`). Defaults are `shkeeper` / `shkeeper`.
- 401 response shape: `{"status": "error", "msg": "authorization requred"}` (typo preserved from source).

**Outbound auth:**
- Tron node — HTTP Basic credentials embedded into `FULLNODE_URL` (`app/connection_manager.py:38-43`).
- Shkeeper backend — header `X-Shkeeper-Backend-Key: {SHKEEPER_BACKEND_KEY}` (`app/wallet_encryption.py:82`, `app/block_scanner.py:173`, `app/tasks.py:528`).
- AML provider — request-level signed token via MD5 of `txid:access_key:access_id` (`app/custom/aml/functions.py:99-100, 119-121`).
- GitHub Releases API — anonymous (`app/api/metrics.py:23`).

**Wallet key encryption (at-rest, on the wallet itself):**
- `cryptography.Fernet` with a key derived from a password via `PBKDF2HMAC(SHA256, length=32, salt=b"Shkeeper4TheWin!", iterations=500_000)` (`app/wallet_encryption.py:157-165`).
- Encryption is enabled/disabled at runtime by polling Shkeeper (`/api/v1/{symbol}/decrypt`); a mismatch between Shkeeper-requested mode and on-disk mode triggers `EncryptionModeMismatch(SystemExit)` (`app/wallet_encryption.py:148-154`). `FORCE_WALLET_ENCRYPTION=true` will encrypt-on-demand (`app/wallet_encryption.py:134-143`).
- Dev-mode override: `DEVMODE_ENCRYPTION_PW` skips the Shkeeper polling (`app/wallet_encryption.py:73-78`).

## Monitoring & Observability

**Error Tracking:**
- None detected. No Sentry, Bugsnag, Rollbar, etc. integrations.
- All exceptions are logged via the project's `logger` (`app/logging.py:1-23`); the Flask blueprint registers a global handler that returns `{"status": "error", "msg": str(e)}` (`app/api/__init__.py:36-41`).

**Metrics:**
- Prometheus — `prometheus-client==0.21.1`. Endpoint `GET /metrics` defined at `app/api/metrics.py:41-58`.
- Default Python collectors disabled: `GC_COLLECTOR`, `PLATFORM_COLLECTOR`, `PROCESS_COLLECTOR` (`app/api/metrics.py:12-14`).
- Custom metrics: `tron_fullnode_status{server}`, `tron_fullnode_version{server}`, `tron_fullnode_last_block{server}`, `tron_fullnode_last_block_ts{server}`, `tron_wallet_last_block`, `tron_wallet_last_block_ts`, `tron_has_alive_servers`, `tron_fullnode_last_release` (Info).

**Logs:**
- Single `logging.StreamHandler` to stderr with format `%(levelname)s %(filename)s:%(lineno)s %(threadName)s %(funcName)s(): %(message)s` (`app/logging.py:6-19`).
- Level driven by `DEBUG` env var (`INFO` otherwise).
- Block-scanner stats logger writes histograms and ETA every `BLOCK_SCANNER_STATS_LOG_PERIOD` seconds (default 300), see `app/block_scanner.py:400-424` and `app/tasks.py:680-696`.

## CI/CD & Deployment

**Hosting:**
- Container image distributed via Docker Hub: `vsyshost/tron-shkeeper`. Deploy target is up to the operator (Docker Compose / Kubernetes / etc.); no compose/k8s manifest in this repo (`docker-compose*` is gitignored at `.gitignore:19`).

**CI Pipeline:**
- `.github/workflows/ci.yml` — on `v*.*.*` git tags: build & push semver-tagged image (`docker/build-push-action@v4`).
- `.github/workflows/dev-ci.yml` — on every push: build & push `dev-{branch}-{sha}` tagged image.
- `.github/workflows/issue_create_auto_reply.yml` — auto-reply bot for new GitHub issues.
- Required GitHub secrets: `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`.

## Environment Configuration

**Required env vars (no defaults will produce a working deployment without these):**
- `FULLNODE_URL` (or `MULTISERVER_CONFIG_JSON`) — Tron node endpoint(s).
- `TRON_NODE_USERNAME`, `TRON_NODE_PASSWORD` — Tron node Basic auth.
- `REDIS_HOST` — Celery broker host.
- `SHKEEPER_HOST`, `SHKEEPER_BACKEND_KEY` — parent Shkeeper integration.
- `BTC_USERNAME` (alias `API_USERNAME`), `BTC_PASSWORD` (alias `API_PASSWORD`) — inbound API auth.

**Optional env vars (with defaults set in `app/config.py:16-79`):**
- `TRON_NETWORK` (`main` | `nile`), `TRON_CLIENT_TIMEOUT`, `DEBUG`.
- `DATABASE`, `DB_URI`, `BALANCES_DATABASE` — DB paths/URIs.
- `INTERNAL_TX_FEE`, `TX_FEE`, `TX_FEE_LIMIT`, `BANDWIDTH_PER_*`, `TRX_PER_BANDWIDTH_UNIT`, `TRX_MIN_TRANSFER_THRESHOLD`, `USDT_MIN_TRANSFER_THRESHOLD`, `USDC_MIN_TRANSFER_THRESHOLD`.
- `BLOCK_SCANNER_*` — scanner tuning.
- `MULTISERVER_CONFIG_JSON`, `MULTISERVER_REFRESH_BEST_SERVER_PERIOD`.
- `FORCE_WALLET_ENCRYPTION`, `DEVMODE_ENCRYPTION_PW`, `DEVMODE_SKIP_NOTIFICATIONS`, `DEVMODE_CELERY_NODELAY`.
- `EXTERNAL_DRAIN_CONFIG` (JSON `ExternalDrain` schema) and AML timing knobs `DELAY_AFTER_FEE_TRANSFER`, `AML_RESULT_UPDATE_PERIOD`, `AML_SWEEP_ACCOUNTS_PERIOD`, `AML_WAIT_BEFORE_API_CALL`.
- `ENERGY_DELEGATION_MODE*` family (7 toggles) at `app/config.py:66-72`.
- `SR_VOTING`, `SR_VOTES` (JSON `List[SrVote]`), `SR_VOTING_ALLOW_BURN_TRX`.
- `CONCURRENT_MAX_WORKERS`, `CONCURRENT_MAX_RETRIES`, `BALANCES_RESCAN_PERIOD`, `SAVE_BALANCES_TO_DB`.

**Secrets location:**
- Loaded by `pydantic_settings.BaseSettings` from process env or `.env` (`app/config.py:14`). `.env` is gitignored (`.gitignore:20`). No secrets manager (Vault, AWS Secrets Manager, etc.) integration. Encryption password is fetched at runtime from Shkeeper backend rather than stored locally (`app/wallet_encryption.py:66-109`).

## Webhooks & Callbacks

**Incoming (REST endpoints, all require Basic auth):**
- `api` blueprint (URL prefix `/<symbol>` — symbol is `TRX`, `USDT`, or `USDC`, set via `before_request` URL preprocessor at `app/api/__init__.py:31-33`):
  - `POST /<symbol>/generate-address` — create one-time deposit address (`app/api/views.py:20-41`).
  - `POST /<symbol>/balance` — fee-deposit account balance (`app/api/views.py:44-54`).
  - `POST /<symbol>/status` — block scanner sync status (`app/api/views.py:57-65`).
  - `POST /<symbol>/transaction/<txid>` — fetch transaction details (`app/api/views.py:68-117`).
  - `POST /<symbol>/dump` — dump all keys for symbol (`app/api/views.py:120-135`).
  - `GET /<symbol>/addresses` — list watched addresses (`app/api/views.py:138-143`).
  - `POST /<symbol>/fee-deposit-account` — fee-deposit account info (`app/api/views.py:146-154`).
  - `POST /<symbol>/estimate-energy/<src>/<dst>/<amount>` — TRC-20 energy estimate (`app/api/views.py:157-161`).
  - `GET /<symbol>/multiserver/status` — multiserver health (`app/api/views.py:169-172`).
  - `POST /<symbol>/multiserver/change/<int:server_id>` — manual server switch (`app/api/views.py:175-184`).
  - `POST /<symbol>/multiserver/switch-to-best` — auto switch to highest block (`app/api/views.py:187-198`).
  - `POST /<symbol>/calc-tx-fee/<amount>`, `POST /<symbol>/multipayout`, `POST /<symbol>/payout/<to>/<amount>`, `POST /<symbol>/task/<id>` — payout flow (`app/api/payout.py:17-92`).
- `metrics_blueprint`: `GET /metrics` (`app/api/metrics.py:41-58`).
- `staking_bp` (URL prefix `/staking`): `GET /staking/info`, `GET /staking/[<address>]`, `POST /staking/freeze/<amount>/<res_type>`, `POST /staking/unfreeze/<amount>/<res_type>`, `POST /staking/withdraw_unfreezed`, `POST /staking/claim_voting_reward`, `POST /staking/withdraw_stake_balance`, `POST /staking/delegate/<address>/<amount>/<res_type>`, `POST /staking/undelegate/<address>/<amount>/<res_type>`, `POST /staking/grant_permissions` (stub) — full surface in `app/api/staking.py:19-260`.

**Outgoing (callbacks the service emits):**
- Shkeeper `walletnotify` — fired by `BlockScanner.notify_shkeeper` for every observed deposit to a watched address (`app/block_scanner.py:167-177`). Skipped when `DEVMODE_SKIP_NOTIFICATIONS=true`.
- Shkeeper `payoutnotify` — fired by Celery task `post_payout_results` after each payout batch completes (`app/tasks.py:522-533`); retries forever with 10s sleep on failure.

---

*Integration audit: 2026-04-30*

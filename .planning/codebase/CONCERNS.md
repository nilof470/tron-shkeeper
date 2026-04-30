# Codebase Concerns

**Analysis Date:** 2026-04-30

This audit of `tron-shkeeper` (Tron blockchain payment gateway processing real money via TRX/USDT-TRC20) groups findings by severity. Many issues compound: the codebase has zero automated tests, broad `except Exception` handlers, and a Celery configuration that uses an unsafe binary serialization format over a Redis broker without authentication — so any single security or correctness bug can have outsized consequences.

---

## CRITICAL

### Celery uses unsafe binary serialization over Redis (RCE risk)

- **Files:** `app/__init__.py:10-18`
- **Issue:** Celery is configured with `task_serializer="pickle"`, `accept_content=["pickle"]`, `result_serializer="pickle"`, `result_accept_content=["pickle"]`. The Redis broker URL is `redis://{config.REDIS_HOST}` (`app/__init__.py:12-13`) with no password, no TLS, no ACL.
- **Impact:** Anyone who can write to the Redis instance (e.g., misconfigured network, container co-tenant, leaked `REDIS_HOST`) can deliver an arbitrary serialized payload that triggers code execution inside the Celery worker — which holds the decrypted master fee-deposit private key in memory. This is a full wallet-drain primitive.
- **Fix approach:** Switch to `task_serializer="json"` everywhere, require `redis://:password@host` with a generated secret, and bind Redis to the container network only. If the unsafe format is genuinely required for some payload (it shouldn't be), sign it with HMAC.

### `Wallet.main_account` cached as a class attribute at import time

- **Files:** `app/wallet.py:14-19`, used in `app/wallet.py:41,67`, `app/api/payout.py:56`, `app/custom/aml/tasks.py:20`
- **Issue:** `main_account = query_db2('select * from keys where type = "fee_deposit" ', one=True)` is executed when the `Wallet` class is first imported. If the fee-deposit key does not exist yet (cold start before `init_wallet`) `main_account` is `None`, and any subsequent attempt to read `self.main_account["public"]` raises `TypeError`. If the fee-deposit key is rotated/replaced at runtime, every already-loaded process keeps using the stale row indefinitely.
- **Impact:** Silent fund routing to the wrong account after key rotation; hard-to-diagnose `NoneType` crashes during bootstrap.
- **Fix approach:** Convert to a property/method that performs a fresh `query_db2` call (or caches behind an explicit cache invalidation hook). Same for `BlockScanner.main_account` (`app/block_scanner.py:92-96`) which is at least decorated with `cached_property` per-instance, but still suffers stale-cache risk.

### `Wallet.transfer` lacks idempotency / replay safety

- **Files:** `app/wallet.py:61-115`, `app/custom/aml/classes.py:101-145`, `app/api/payout.py:78-83`, `app/api/payout.py:22-75`
- **Issue:** A payout is built, signed, and broadcast in a single function call. There is no persisted "payout intent" row that records (idempotency_key, dst, amount, status) before signing. If the Celery task is retried (e.g., worker restart, `broadcast().wait()` raises after the tx is actually accepted by the node, or the API client retries `/payout`/`/multipayout`), nothing prevents re-signing and re-broadcasting a *new* transaction with a fresh nonce that pays the destination twice.
- **Impact:** Double-payment of real funds. The 12-hour expiration extension (`app/wallet.py:84`) makes the window even larger. `concurrent.futures.ThreadPoolExecutor` in `payout` (`app/tasks.py:78-83`) compounds this — partial failure of one thread leaves no recovery state.
- **Fix approach:** Persist an idempotency record before `txn.broadcast()`, key it on the API request id (or a deterministic hash of `dst`/`amount`/`nonce`), check it on retry, and surface the on-chain txid back through the same key. AML payouts already write a `Payout` row (`app/custom/aml/classes.py:121-134`) but only *after* broadcast — flip the order.

### `txn._raw_data["expiration"] += 12 * 60 * 60 * 1_000` (12-hour signed-tx window)

- **Files:** `app/wallet.py:83-84`
- **Issue:** Comment links to a 2019 java-tron issue. Extending the expiration to 12 hours means a signed-but-unconfirmed transaction can be re-broadcast or confirmed up to 12 hours later. Combined with no idempotency record, a transient broadcast error can cause a tx to confirm long after the caller assumed it had failed and retried.
- **Impact:** Late-confirming "ghost" transactions that double-spend with the user-visible payout result. Given Tron's fast block time, a 12-hour expiration is two orders of magnitude longer than necessary.
- **Fix approach:** Reduce to a few minutes (the rest of the codebase uses `current_timestamp() + 60_000`, e.g. `app/tasks.py:267,389,404,512`). Keep payout tx expirations in line with internal-fee tx expirations.

### Hardcoded PBKDF2 salt for wallet encryption KDF

- **Files:** `app/wallet_encryption.py:156-165`
- **Issue:** `salt = b"Shkeeper4TheWin!"` is a fixed, public, 16-byte string. PBKDF2-SHA256 with 500k iterations becomes equivalent across all deployments — anyone who exfiltrates a `keys.private` ciphertext can grind a single rainbow table (or distributed wordlist attack) and reuse it against every Shkeeper installation.
- **Impact:** If a database backup leaks (cloud snapshot, dev box, support bundle), offline brute-force becomes order-of-magnitude cheaper than per-instance attacks. With weak operator passwords, full key recovery.
- **Fix approach:** Generate a per-deployment random salt at first encryption setup, store it alongside the encrypted blob (or in the `settings` table), and pass it through to `_get_key_from_password`. Migrate existing rows by re-encrypting under the new salt during the same one-time pass `encrypt_db` already does.

### `/dump` endpoint returns all decrypted private keys

- **Files:** `app/api/views.py:120-135`
- **Issue:** `POST /<symbol>/dump` decrypts every key (`wallet_encryption.decrypt(row["private"])`) and returns them in JSON. Auth is HTTP Basic via `API_USERNAME`/`API_PASSWORD` (`app/api/__init__.py:13-23`) defaulting to `shkeeper`/`shkeeper` (`app/config.py:30-32`). There is no IP allow-list, no rate limit, and no TLS enforcement at this layer.
- **Impact:** A single leaked basic-auth header dumps the complete wallet — fee-deposit key plus every onetime key. Logging a request URL with credentials (some load balancers do) is sufficient to lose all funds.
- **Fix approach:** Require non-default credentials (refuse to start with `shkeeper`/`shkeeper`), add a separate "export" privilege gated by an additional environment-controlled token, log every call with operator IP, and consider streaming an encrypted bundle instead of cleartext keys.

### Block scanner is a separate thread but reads/writes the same SQLite DB as Flask requests

- **Files:** `run.py:33-48`, `app/block_scanner.py:31-68`, `app/db.py:35-72`, `app/api/views.py:20-41`
- **Issue:** `BlockScanner` runs in a daemon thread inside the same process as Gunicorn-served Flask. It reads/writes `WATCHED_ACCOUNTS` (a class-level `set`) without a lock. `add_watched_account` (`app/block_scanner.py:82-86`) and `set_watched_accounts` (`app/block_scanner.py:74-79`) can race with `get_watched_accounts` (`app/block_scanner.py:70-72`) used by `scan` and `views.get_transaction` (`app/api/views.py:85,97,107`). Python's GIL makes `set.add` atomic for CPython today, but membership testing during a concurrent `set()` reassignment is undefined for callers iterating the result.
- **Impact:** A newly generated address (`POST /generate-address`, `app/api/views.py:20-41`) can be missed by an in-flight block scan, causing the gateway to *not notify Shkeeper* about an incoming deposit. With Gunicorn's multi-process worker model, `WATCHED_ACCOUNTS` lives only in the request-handling worker — the block-scanner thread runs in *one* worker, but `add_watched_account` is called from *whichever* worker handled the API call. The address is added to one process's set, while the other process scans blocks against a stale set.
- **Severity:** Lost deposit notifications until process restart re-loads from DB. This is happening today wherever `gunicorn --workers > 1`.
- **Fix approach:** Move `WATCHED_ACCOUNTS` out of process memory entirely — either re-query the `keys` table on each block (cheap with an index), or maintain it in Redis. Run the block scanner as its own process (already half-done via Celery infrastructure) rather than a thread inside a web worker.

### `setup_periodic_tasks` is conditional on a config flag at worker startup

- **Files:** `app/tasks.py:804-817`
- **Issue:** `if config.EXTERNAL_DRAIN_CONFIG:` chooses between AML periodic tasks and `scan_accounts`. There is no migration / coexistence path: switching modes after deployment leaves stale schedules in `celerybeat-schedule` and orphan in-flight tasks.
- **Impact:** Operators flipping AML mode mid-flight risk silent loss of `scan_accounts` runs (so onetime balances stop being swept) or duplicate scheduling.
- **Fix approach:** Document the mode switch as a "drain-first, then redeploy" procedure, or unify the two schedules.

### Raw private keys handled via string concatenation / hex parsing without zeroization

- **Files:** `app/wallet.py:85-87`, `app/tasks.py:493-503,793-794`, `app/utils.py:78-79`, `app/api/views.py:30-31`
- **Issue:** Decrypted private keys are passed around as `str` and `bytes`, multiplied by intermediate function calls, and live in tracebacks/log records (e.g., when `.broadcast().wait()` raises). Python strings are immutable — there is no clean way to scrub them — and broad `except Exception` handlers (`app/block_scanner.py:64,308`, `app/tasks.py:531,676`) format and log the entire context, potentially including locals via `logger.exception`.
- **Impact:** Memory dumps, core files, logs, or APM traces may contain decrypted private keys. Combined with the `/dump` exposure and Redis-broker path, attack surface for key extraction is large.
- **Fix approach:** Wrap private keys in a single bounded scope (load → sign → drop) and use `del` aggressively. Audit `logger.exception` call sites and ensure `extra=` is used for redactable fields. Avoid f-string formatting transactions before catching exceptions.

### Default API credentials `shkeeper`/`shkeeper` and `SHKEEPER_BACKEND_KEY="shkeeper"`

- **Files:** `app/config.py:30-32`
- **Issue:** All three secrets — `API_USERNAME`, `API_PASSWORD`, `SHKEEPER_BACKEND_KEY` — default to the literal string `"shkeeper"`. Only env-var override changes them. There is no startup-time refusal to boot with defaults.
- **Impact:** Combined with `/dump`, a default-credentialed deployment is trivially drainable. The `SHKEEPER_BACKEND_KEY` is also the credential the encryption-key fetch uses (`app/wallet_encryption.py:80-83`), so a leak compromises the password too.
- **Fix approach:** Refuse to boot if any of these match the literal `"shkeeper"`. Document that operators must rotate at install time.

---

## IMPORTANT

### `scan_accounts` retry loop is a no-op due to operator precedence bug

- **Files:** `app/tasks.py:588-602`, `app/tasks.py:632-647`
- **Issue:** `while ret := 0 < config.CONCURRENT_MAX_RETRIES:` parses as `ret := (0 < config.CONCURRENT_MAX_RETRIES)`, i.e. `ret := True` (since `CONCURRENT_MAX_RETRIES` defaults to 10, `app/config.py:22`). The body never increments `ret` correctly: `ret += 1` makes `ret = True + 1 = 2`, then on next iteration `ret := 0 < 10 = True` again — the counter is *clobbered every iteration*. The `else` clause of `while` (`app/tasks.py:599-602`) therefore *never* runs because the loop condition is constant `True`. On a persistent error, the loop spins forever inside one Celery task.
- **Impact:** A flaky token contract call can hang `scan_accounts` indefinitely, blocking all subsequent onetime sweeps.
- **Fix approach:** Replace with `for ret in range(config.CONCURRENT_MAX_RETRIES): try: ... break; else: raise`. Same fix pattern in both retry blocks.

### `is_task_running` check passes wrong task name in TRX path

- **Files:** `app/tasks.py:705-715`
- **Issue:** Loop dispatches `transfer_trx_from(account)` but checks `is_task_running(self, "app.tasks.transfer_trc20_from", args=[account])`. The TRC20 task name does not match what TRX dispatch would register, so the de-dupe check is ineffective for TRX transfers. Additionally `transfer_trx_from(account)` is called *synchronously* from inside `scan_accounts` (no `.delay()`), serializing the entire periodic scan.
- **Impact:** Duplicate TRX sweep tasks under load. Long `scan_accounts` runtime (each TRX transfer waits on `broadcast().wait()`).
- **Fix approach:** Pass `"app.tasks.transfer_trx_from"` as the task name and use `.delay(account)` to dispatch asynchronously.

### `transfer_trc20_from` invoked synchronously inside `scan_accounts`

- **Files:** `app/tasks.py:697-703`
- **Issue:** `transfer_trc20_from(account, symbol)` is called as a plain function rather than `.delay()`. In a Celery worker context this runs the entire transfer flow inside the periodic-scan task, including blocking `broadcast().wait()` calls.
- **Impact:** A periodic scan that finds N onetime accounts with balances takes O(N × tx-confirmation-time). The `BALANCES_RESCAN_PERIOD` (3600 s default) easily wraps around itself.
- **Fix approach:** Use `transfer_trc20_from.delay(...)` (the same code already does this from `block_scanner.scan` at `app/block_scanner.py:296-298`).

### `get_transaction` returns duplicate entries for internal transfers

- **Files:** `app/api/views.py:68-117`
- **Issue:** Three `if` blocks (not `elif`) mean an internal tx (`src_addr` and `dst_addr` both watched) appends to `result` three times: once `internal`, once `receive`, once `send`. Shkeeper consumers parsing this list will likely double-count.
- **Impact:** Accounting inconsistencies between Shkeeper UI and on-chain state.
- **Fix approach:** Convert the three `if`s into an `if/elif/elif` chain; keep `internal` mutually exclusive with the other two, or document the contract explicitly.

### `transfer_trc20_from` references undefined `energy_needed` outside its delegation branch

- **Files:** `app/tasks.py:161,175,177` (inside `delegate_energy` closure), and `app/tasks.py:286-292` (outer function)
- **Issue:** The `energy_needed` log lines on lines 161 and 175 are inside `delegate_energy()` defined at `app/tasks.py:122-184`. `delegate_energy` is invoked via `delegate_energy(sun_to_delegate)` (`app/tasks.py:354`) but `energy_needed` is captured from the enclosing function scope via Python closure rather than passed as an argument. If `delegate_energy` is ever invoked before `energy_needed` is set (e.g., a future refactor moves the call), it raises `UnboundLocalError` and aborts mid-transfer.
- **Impact:** Latent NameError waiting to fire after refactoring; brittle code review.
- **Fix approach:** Pass `energy_needed` as an explicit parameter to `delegate_energy`.

### `/multipayout` warns on insufficient token balance but proceeds anyway

- **Files:** `app/api/payout.py:46-50`
- **Issue:** `if balance < need_tokens: pass` — the `raise` is commented out (`app/api/payout.py:50`).
- **Impact:** A multi-payout that exceeds the wallet balance is queued and partial-executed: the first N transfers succeed, then later ones fail. Each individual `wallet.transfer` (`app/tasks.py:81-82`) will raise from inside the thread pool, and partial payouts are not reversed.
- **Fix approach:** Re-enable the balance check, or document explicit partial-success semantics and surface a structured error per failed leg back through the task result.

### AML payout split has off-by-one and rounding bugs in last-recipient logic

- **Files:** `app/custom/aml/functions.py:189-199`, `app/custom/aml/functions.py:251-261`
- **Issue:** `for i in range(len(external_drain_list) - 1):` deliberately excludes the last entry so the remainder can absorb rounding error. But `external_drain_list[-1][1] = the_rest` overwrites the *ratio* with the *amount*, then `external_drain_list[-1].append(the_rest)` appends a third element. Earlier entries keep `[address, amount, amount]`; the last entry becomes `[address, ratio, the_rest]` initially, then the index-1 slot is overwritten to `the_rest` and a third slot of `the_rest` is appended. The downstream consumer `external_drain_list[i][1]` (e.g., `app/custom/aml/classes.py:53`) then gets the same value but the third tuple element (`orig_amount` per `app/custom/aml/classes.py:106`) is now `the_rest` not the unmodified ratio.
- **Impact:** Reporting (`amount_calc` in `Payout` row, `app/custom/aml/classes.py:127`) is wrong for the last destination. If `the_rest` ever evaluates negative due to rounding (e.g., dust > sum of explicit ratios), the gateway broadcasts a transfer that will fail with `tronpy.exceptions.ValidationError` (`app/custom/aml/classes.py:113-118`) and abort the whole payout halfway through.
- **Fix approach:** Compute amounts explicitly with `Decimal.quantize`, validate each leg `> 0`, and persist the original ratio separately from the resolved amount.

### `aml_check_transaction` hardcodes `symbol = "TRX"` regardless of caller intent

- **Files:** `app/custom/aml/functions.py:97-115`
- **Issue:** Line 98 sets `symbol = "TRX"` and the function ignores any USDT/USDC context. AMLBot is told that *every* deposit is a TRX deposit, even when the actual asset is USDT-TRC20.
- **Impact:** AML scoring may be inaccurate (different risk profiles for stablecoins vs native asset). Audit/reporting trail is wrong.
- **Fix approach:** Accept `symbol` as a function parameter, plumb it through the call sites in `app/custom/aml/tasks.py:34`, and translate `TronSymbol` enum to AMLBot's expected string per token.

### Race between `setup_encryption()` and Flask boot

- **Files:** `run.py:1-21`, `celery_worker.py:1-10`, `app/__init__.py:50-56,61-66`
- **Issue:** `wallet_encryption.setup_encryption()` blocks indefinitely waiting for Shkeeper to provide a password (`app/wallet_encryption.py:69-109`). It runs inline at module-import time. Then `create_app()` immediately reads the (presumably encrypted) `keys` table to build `WATCHED_ACCOUNTS` and to fetch the fee-deposit account. If `setup_encryption` is bypassed in tests or during a partial boot, decryption fails silently inside `Wallet.transfer`.
- **Impact:** Hard-to-test boot sequence; long boot times during Shkeeper outages; tight coupling to a remote HTTP service.
- **Fix approach:** Make encryption setup async with explicit health-check states and refuse to register the API blueprint until encryption is `OK`. Provide a separate read-only mode that does not require keys.

### `query_db2` opens a new SQLite connection for every call

- **Files:** `app/db.py:61-72`
- **Issue:** Every `query_db2` invocation calls `sqlite3.connect`, sets WAL mode, runs a single statement, and closes implicitly. The block scanner calls this inside its hot loop (`get_last_seen_block_num` per chunk, `set_last_seen_block_num` per chunk). The same applies to AML tasks running across many onetime accounts.
- **Impact:** File-handle churn on the SQLite database; under load the connection-open syscalls dominate time. The WAL pragma is run on every connection unnecessarily.
- **Fix approach:** Introduce a thread-local connection cache or migrate to SQLAlchemy's pool (the existing `engine` already exists for SQLModel tables — unify the two paths).

### Two `RegularSplitConfig` classes shadow each other

- **Files:** `app/custom/aml/schemas.py:28-40`
- **Issue:** Two classes named `RegularSplitConfig` are defined back-to-back. The second (line 36) overrides the first (line 28). The "outer" wrapper at line 36 has a `cryptos: dict[..., RegularSplitConfig]` annotation that *self-references the wrapper*, not the per-token shape. Pydantic resolves it, but the resulting type is recursive and the field expecting `addresses` actually expects another wrapper.
- **Impact:** External-drain config validation may accept malformed configurations or reject valid ones; subtle.
- **Fix approach:** Rename the inner class (e.g., `RegularSplitCryptoConfig`) to match the AML side (`AmlCryptoConfig`).

### `current_app.config.DATABASE` access via private attribute

- **Files:** `app/db.py:38`
- **Issue:** `g.db = sqlite3.connect(current_app.config.DATABASE, …)` works only because of the `AttrConfig` shim in `app/__init__.py:29-39`. This is fragile; any code path that hits `Flask.config[...]` style elsewhere is fine, but the `AttrConfig` is set globally on `Flask.config_class` which affects every Flask app in the process.
- **Impact:** Confusing for new contributors; surprises in test environments that import Flask separately.
- **Fix approach:** Read `config.DATABASE` from the Pydantic settings module directly (already imported at `app/db.py:8`).

### Block scanner per-instance LRU cache size pegged to chunk size

- **Files:** `app/block_scanner.py:147-165`
- **Issue:** `@functools.lru_cache(maxsize=config.BLOCK_SCANNER_MAX_BLOCK_CHUNK_SIZE)` is evaluated at decoration time. If `BLOCK_SCANNER_MAX_BLOCK_CHUNK_SIZE` is 1 (default, `app/config.py:47`), the cache holds exactly one block — defeating the purpose. Worse, the cache key includes `self`, so re-instantiating `BlockScanner` (e.g., in tests, or whenever `BlockScanner()` is called fresh inside `views.get_status` `app/api/views.py:59`) creates a separate cache and re-downloads.
- **Impact:** Wasted JSON-RPC calls; cache effectively useless because `download_block(n)` is called from a single thread per block.
- **Fix approach:** Either move caching to Redis or remove it entirely if calls are already de-duplicated upstream.

### `transfer_trc20_from` is a 280+ line task with deeply nested branches

- **Files:** `app/tasks.py:88-418`
- **Issue:** Two energy modes interleaved with activation, fee transfer, energy estimation, delegation, and undelegation. The function holds two inline closures (`calc_sun_for_energy_delegation`, `delegate_energy`) and reuses outer-scope variables. Reading the control flow requires tracking ~10 sequential conditionals.
- **Impact:** Hard to test, hard to extend, and changes in one mode risk breaking the other (the recent commits "Skip transfer from main account" and "Check onetime bandwidth in delegation mode only" all touched this block).
- **Fix approach:** Decompose into an `EnergyProvider` strategy interface (delegation vs burn-trx) and pull each mode into its own well-typed function. The existing `Wallet` class is the natural home for shared parts.

---

## MINOR

### `claim_reward` is a no-op stub

- **Files:** `app/tasks.py:779-801`
- **Issue:** Entire body is commented out; only a `# TODO: implement automatic reward claims` (`app/tasks.py:782`) and a final `pass`. The task is registered.
- **Impact:** Voting rewards accumulate on-chain unclaimed; manual `/staking/claim_voting_reward` (`app/api/staking.py:184-193`) needed.
- **Fix approach:** Implement reward claim against the energy delegator account, gated on `acc_info["allowance"] > threshold` and the 24-hour cool-down.

### `grant_permissions` endpoint is empty

- **Files:** `app/api/staking.py:250-260`
- **Issue:** `pass` — function body is just a docstring.
- **Impact:** Dead route, returns `null`; user expectations from the docstring won't match runtime behavior.
- **Fix approach:** Implement the AccountPermissionUpdate flow, or remove the route until ready.

### `# time.sleep(10)  # FIXME` in AML payout loop

- **Files:** `app/custom/aml/classes.py:145`
- **Issue:** Commented-out delay between consecutive AML payouts. Comment in surrounding code (`app/custom/aml/classes.py:96-99`) suggests a similar removed `time.sleep(config.DELAY_AFTER_FEE_TRANSFER)` after the fee transfer. If the fee-deposit-funded TRX hasn't yet propagated when the TRC20 transfer broadcasts, the broadcast can fail validation.
- **Impact:** Intermittent payout failures under high load when the same node is queried before its mempool has the funding tx.
- **Fix approach:** Replace the sleep with an active wait that polls `get_account_resource` until the funding tx is confirmed, with a short timeout.

### `tron_client.get_account_resource(onetime_publ_key)` called twice in error path

- **Files:** `app/tasks.py:272-283`
- **Issue:** Lines 272-274 fetch resources, then lines 275-282 fetch them again wrapped in a `try/except`. The first call is unconditional and outside the try, so its `AddressNotFound` would propagate before the catch block can run.
- **Impact:** Cosmetic / dead code; the activation-check error message is unreachable.
- **Fix approach:** Remove the unconditional call at lines 272-274.

### `ConnectionManager.get_servers_status()` constructs URL with embedded credentials

- **Files:** `app/connection_manager.py:36-43`, `app/connection_manager.py:84-104`
- **Issue:** `server.url` includes `username:password@host` in the URL. `requests.get(f"{server.url}/wallet/getnodeinfo")` passes credentials via the URL — they appear in `RequestException.request.url` if the call fails, and in `requests` library debug logging.
- **Impact:** TRON node Basic-Auth credentials (`TRON_NODE_USERNAME`/`TRON_NODE_PASSWORD`, `app/config.py:27-28`) leak into error logs.
- **Fix approach:** Store creds separately and pass `auth=(user, pw)` to `requests.get`.

### `get_servers_status` mutates the response inline (`del node_info["peerList"]`)

- **Files:** `app/connection_manager.py:91-93`
- **Issue:** Hard `del` of `peerList` and `machineInfo.memoryDescInfoList`. If a future Tron node release renames or removes those keys, the call raises `KeyError` *inside* the try/except and the entire server is reported as offline despite being healthy.
- **Impact:** False negatives in failover decisions during node upgrades.
- **Fix approach:** Use `node_info.pop(key, None)` and tolerate missing fields.

### Verbose error messages bubble Tron-internal details to API clients

- **Files:** `app/api/__init__.py:36-41`, callers throughout `app/api/views.py`, `app/api/payout.py`
- **Issue:** Global handler returns `str(e)` which often includes stack-frame-level internals from tronpy or Pydantic validators (e.g., `is_tron_address`, `app/schemas.py:38-43`).
- **Impact:** UX confusion; potential information disclosure.
- **Fix approach:** Categorize exceptions and emit a curated `{"status":"error","msg": ...}` per category.

### `time.sleep(10)` retry inside Celery task

- **Files:** `app/tasks.py:524-533`
- **Issue:** `post_payout_results` infinitely loops on `requests.post` failure with a 10-second sleep, blocking the Celery worker's slot.
- **Impact:** Long Shkeeper outages stall the payout-notification queue and effectively serialize every subsequent payout's notification step.
- **Fix approach:** Use Celery's native `retry` mechanism with exponential backoff and a bounded retry count.

### `signed_tx.inspect()` log call has no return value asserted

- **Files:** `app/api/staking.py:148,164,178,190,204`
- **Issue:** `signed_tx.inspect()` is called but its result is discarded; it appears to be there for side-effects (printing). For an HTTP endpoint, this means stdout noise rather than structured logging, and its absence wouldn't be detected by tests.
- **Impact:** Cosmetic / observability.
- **Fix approach:** Either remove or wrap with `logger.debug(signed_tx.inspect())`.

### `INTERNAL_TX_FEE` and `TX_FEE` defaults of 40 TRX hard-coded as `Decimal`

- **Files:** `app/config.py:34-36`
- **Issue:** Operators who don't override these with env vars pre-fund 40 TRX per onetime sweep. Worse, `multipayout` reserves `len(payout_list) * 40 TRX` (`app/api/payout.py:52`), refusing to dispatch perfectly affordable batches.
- **Impact:** Operator confusion; rejected legitimate payouts.
- **Fix approach:** Tune defaults to current fee-market reality and document override semantics. Compute energy/bandwidth dynamically before reservation.

### Logging includes full account public keys (no PII reduction)

- **Files:** `app/wallet.py:91-93`, `app/tasks.py:186,261,475,517` (and many others)
- **Issue:** Every log line emits full Tron addresses. While these are public on-chain anyway, log aggregation indexing them links operator-scoped flows to each other and to balances/amounts, simplifying side-channel deanonymization of the operator's customer base.
- **Impact:** Privacy regression for end-users.
- **Fix approach:** Use `short_txid`-style truncation for routine info-level lines, full address only at debug level.

### No formatter, linter, or type checker configured

- **Files:** project-wide (no `pyproject.toml`, `.flake8`, `mypy.ini`)
- **Issue:** Style is mostly consistent organically, but nothing enforces it. `requirements.txt` has no dev tools.
- **Impact:** Drift over time; new contributors must reverse-engineer style.
- **Fix approach:** Adopt `ruff` + `black` + `mypy --strict` in a single dedicated PR. Avoid mixing reformat noise with feature work.

### Type hints sparse

- **Files:** older modules `app/tasks.py`, `app/wallet.py` lack hints; newer `app/utils.py:66-79,100-114` has them
- **Issue:** Inconsistent annotation discipline.
- **Impact:** Reduced IDE support; harder onboarding.
- **Fix approach:** Add type hints in **new** code as touched. Don't retrofit older files in a single sweep.

---

## Test Coverage Gaps

**No automated tests exist anywhere in the repository.** `find . -name 'test_*' -o -name '*_test.py'` returns nothing under the project tree (excluding `.venv`). There is no `pytest.ini`, no `tox.ini`, no `conftest.py`, no `tests/` directory, no testing dependency in `requirements.txt`. CI (`.github/workflows/ci.yml`, `.github/workflows/dev-ci.yml`) only builds and pushes Docker images — no test step.

**Untested critical paths:**

- **Transaction signing** — `app/wallet.py:61-115` and the broadcast/sign flows in `app/tasks.py`. No test verifies the encryption round-trip, expiration extension, or fee-limit application.
- **Block scanner deduplication** — `app/block_scanner.py:179-312` parsing logic. Edge cases like multi-log TRC20 transfers, contract self-destructs, and mixed-success blocks are unproven.
- **Encryption mode mismatch** — `app/wallet_encryption.py:111-154`. The "exit to prevent corruption" branch is purely operational; no test guards against accidental refactor.
- **AML payout splitter** — `app/custom/aml/functions.py:132-275`. Off-by-one fixes here would land blind without tests.
- **Concurrency** — `WATCHED_ACCOUNTS` set, the `Wallet.main_account` class attribute, `download_block` LRU cache.
- **Multi-server failover** — `app/connection_manager.py:137-178`. Behavior under "all servers offline" or "best server flapping" is unverified.
- **Idempotency / retry** — once introduced (see Critical), tests must cover Celery retry + DB state.
- **Settings validation** — `app/config.py:152-166` (EXTERNAL_DRAIN_CONFIG validator) untested.

**Priority:** High — for a real-money gateway, the bar should be at least integration tests against a Tron testnet (Nile) node and unit tests around the parser, encryption, and split arithmetic. Recommend adding a `tests/` directory with `pytest`, fixtures for in-memory SQLite, and a mocked tronpy provider before any new feature work.

---

## Dependencies at Risk

- **`tronpy==0.5.0`** (`requirements.txt`) — Pinned to a single minor; check upstream for security advisories. The 12-hour expiration workaround quoted in `app/wallet.py:83` references a 2019 bug that may have been resolved.
- **Unsafe Celery serialization over Redis** — see Critical section; this is more an architectural risk than a dependency one.
- **`flask==3.1.0`, `gunicorn==23.0.0`** — current as of this audit; ensure the Dockerfile (`Dockerfile:1`) Python 3.13 base is patched on each release.
- **`pymysql==1.1.1`** — listed in `requirements.txt` but the codebase uses SQLite (`app/config.py:18-20`). Likely vestigial; remove if unused.

---

## Fragile Areas Summary

| Area | Files | Fragility |
|------|-------|-----------|
| `Wallet.main_account` class-level cache | `app/wallet.py:19` | Stale after key rotation; `None` at cold boot |
| `BlockScanner.WATCHED_ACCOUNTS` (in-process set) | `app/block_scanner.py:29` | Lost across worker processes; race with API |
| `Wallet.CACHE` (decimals/contracts) | `app/wallet.py:15-18` | Class-level dict, never invalidated |
| Celery unsafe-format + Redis | `app/__init__.py:10-18` | RCE primitive |
| 12-hour signed-tx expiration | `app/wallet.py:84` | Replay/late-confirm window |
| `query_db2` per-call connection | `app/db.py:61-72` | High file-handle churn |
| Inline `setup_encryption()` at import | `run.py:10`, `celery_worker.py:2` | Boot blocking on remote service |
| Non-atomic AML payout (broadcast then DB write) | `app/custom/aml/classes.py:111-134` | Crash mid-payout = no record |
| `transfer_trc20_from` length & nesting | `app/tasks.py:88-418` | Difficult to safely modify |

---

## Scaling Limits

- **SQLite as the system of record** (`app/config.py:18-20`, `app/db.py:35-72`). Single-writer model. Block-scanner thread + Flask workers + Celery workers all hammer the same `data/database.db` file. Acceptable up to a few thousand onetime accounts; beyond that, contention on the `keys` table dominates.
- **`scan_accounts` is O(N × tokens)** (`app/tasks.py:570-715`) and runs every `BALANCES_RESCAN_PERIOD` (default 3600 s). Each onetime account costs at least one TRC20 RPC call per token plus a TRX balance call. With hundreds of onetime accounts, the period must grow.
- **`BLOCK_SCANNER_MAX_BLOCK_CHUNK_SIZE = 1`** default (`app/config.py:47`) gives single-block-per-iteration throughput. The ThreadPoolExecutor's `max_workers` is bound to this value (`app/block_scanner.py:32-33`), so increasing it scales fanout linearly only up to the JSON-RPC node's rate-limit.
- **Celery `pool=solo`** is referenced in `app/db.py:24-29` as an alternative to NullPool. The current setup uses NullPool which means every Celery task opens its own SQLAlchemy connection — fine for low concurrency, expensive at scale.

---

## Missing Critical Features

- **Idempotency keys on payout endpoints** (`app/api/payout.py:22,78`). Without them, a client retry on connection drop can double-pay. (See Critical.)
- **Persisted wallet event log.** All transfers happen as one-shot Celery tasks; only AML mode writes a `Payout` row. Default mode has no audit trail beyond on-chain.
- **Reorg handling.** `BlockScanner` advances `last_seen_block_num` (`app/block_scanner.py:119-127`) on success and never rewinds. A Tron reorg (rare but possible on testnet) would silently miss the canonical chain.
- **Health check for encryption state.** No `/healthz` distinguishes "encryption not yet initialized" from "wallet ready". Ops cannot wait on the right signal.
- **Rate limiting / abuse protection** on `/generate-address` (`app/api/views.py:20-41`) — an attacker with valid basic auth can balloon the `keys` table indefinitely, exhausting SQLite write performance and growing watched-account set unboundedly.

---

*Concerns audit: 2026-04-30*

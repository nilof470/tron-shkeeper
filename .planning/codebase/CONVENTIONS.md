# Coding Conventions

**Analysis Date:** 2026-04-30

## Naming Patterns

**Files:**
- Lowercase, snake_case: `block_scanner.py`, `connection_manager.py`, `wallet_encryption.py`
- One module per concern (no aggregating barrel files): `tasks.py`, `views.py`, `payout.py`
- Module-as-package: every directory under `app/` (e.g. `app/api/`, `app/custom/aml/`) carries an `__init__.py`. Entry files import the module's blueprints/celery instance there.

**Functions:**
- snake_case for all functions: `get_balance`, `transfer_trc20_from`, `prepare_multipayout`
- Verb-led names for actions: `add_watched_account`, `set_last_seen_block_num`, `notify_shkeeper`
- Private/internal helpers prefixed with single underscore: `_encrypt`, `_decrypt`, `_is_noop`, `_fetch_encryption_settings` (`app/wallet_encryption.py`)

**Variables:**
- snake_case for locals/attributes: `tron_client`, `onetime_publ_key`, `energy_delegator_priv`
- ALL_CAPS for module-level / class-level constants and Pydantic settings: `WATCHED_ACCOUNTS`, `CACHE`, `INTERNAL_TX_FEE`, `BANDWIDTH_PER_TRC20_TRANSFER_CALL` (`app/config.py:13-103`, `app/block_scanner.py:29`, `app/wallet.py:15-18`)
- Class-level cache dicts named `CACHE` (`app/wallet.py:15`)

**Types / Classes:**
- PascalCase: `BlockScanner`, `ConnectionManager`, `Wallet`, `AmlWallet`, `Settings`
- Pydantic models also PascalCase: `Token`, `TronTransaction`, `TronFullnode`, `ExternalDrain`, `AmlSplitConfig`
- SQLModel tables: PascalCase singular (`Key`, `Balance`, `Setting`, `Transaction`, `Payout`) with explicit `__tablename__` strings prefixed `tron_*` (`app/models.py:13`, `app/custom/aml/models.py:11`)
- Enums use PascalCase classnames with lowercase string values: `KeyType.fee_deposit = "fee_deposit"`, `TronNetwork.mainnet = "main"` (`app/schemas.py:10-19`)
- Custom exceptions end in error/intent suffix and inherit from `Exception` directly: `UnknownToken`, `NotificationFailed`, `BadContractResult`, `AllServersOffline`, `NoServerSet` (`app/exceptions.py`)

**Lowercase class names (intentional):**
- `wallet_encryption` (`app/wallet_encryption.py:27`) is a class but written in snake_case because it is consumed as a module-style namespace (`wallet_encryption.encrypt(...)`). Keep this convention when adding new classmethod-only "namespace" containers.

## Code Style

**Formatting:**
- No formatter config committed (no `pyproject.toml`, `.flake8`, `ruff.toml`, `setup.cfg`, `.pre-commit-config.yaml`).
- De-facto style is PEP 8 with 4-space indentation; lines wrap around the standard 88-char Black width (e.g. `app/tasks.py:60`, `app/api/payout.py:55`).
- Trailing commas used on multi-line argument lists and collection literals: `app/config.py:81-103`, `app/api/payout.py:35-40`. Treat this as the in-tree style; apply Black-style wrapping when adding new code.

**Linting:**
- No linter is enforced in CI (`.github/workflows/ci.yml`, `.github/workflows/dev-ci.yml` only build/push Docker images).
- One `# noqa` directive in active code: `app/db.py:6` (`from sqlmodel import SQLModel, create_engine  # noqa: F401`) and `app/db.py:10` (`from .custom.aml import models  # noqa: F401, F811`) used to keep side-effect imports that register SQLModel tables.

## Import Organization

**Order observed (consistent across most modules):**
1. Standard library, alphabetically: `import collections`, `import concurrent`, `from contextlib import closing`, `import datetime`, ... (`app/tasks.py:1-12`)
2. Third-party libraries: `from celery import Celery`, `from pydantic import TypeAdapter`, `import tronpy.exceptions`, `from sqlmodel import Session, select` (`app/tasks.py:14-22`)
3. Local imports, package-absolute first, then relative:
   ```python
   from app.schemas import KeyType        # absolute
   from . import celery                   # relative
   from .config import config
   from .db import query_db, query_db2
   ```
   (`app/tasks.py:24-39`)

**Mixed absolute/relative imports:**
The codebase mixes `from app.X import Y` and `from .X import Y` in the same file (`app/tasks.py:24` vs `app/tasks.py:27-39`, `app/api/staking.py:4-5` vs `app/api/staking.py:7-12`). Prefer relative imports inside the `app` package; only fall back to absolute when needed to break circularity.

**Late / function-local imports (intentional):**
Used to avoid circular imports between `block_scanner`, `tasks`, and AML modules. Examples:
- `from .tasks import transfer_trc20_from, transfer_trx_from` inside `BlockScanner.scan` (`app/block_scanner.py:180`)
- `from .custom.aml.tasks import sweep_accounts, recheck_transactions` inside `setup_periodic_tasks` (`app/tasks.py:810`)
- `from .schemas import SrVote` inside `vote_for_sr` (`app/tasks.py:736`)
- `from .functions import build_payout_list` inside `AmlWallet.payout_for_tx` (`app/custom/aml/classes.py:19`)

When adding cross-module functionality that risks a cycle, follow this same pattern: keep the import at the top of the function body.

**Path Aliases:**
- None. Imports use either bare `app.X` or relative `.X` / `..X`.

## Type Hints

**Where used:**
- Pydantic models in `app/schemas.py`, `app/custom/aml/schemas.py`, `app/config.py`: fully annotated with `Annotated[...]`, `Literal[...]`, and validators.
- SQLModel table classes in `app/models.py` and `app/custom/aml/models.py`: every column annotated.
- Public helpers with explicit return types: `get_key(...) -> tuple[PrivateKey | None, str]` (`app/utils.py:66`), `get_energy_delegator() -> tuple[PrivateKey, str]` (`app/utils.py:100`), `parse_tx(tx, tx_info) -> List[TronTransaction]` (`app/block_scanner.py:315`).
- Modern PEP 604 unions used throughout: `str | None`, `int | None` (`app/config.py:49,56`, `app/utils.py:66`).
- `Annotated` + Pydantic validators define newtypes once and reuse them: `TronAddress = Annotated[str, AfterValidator(is_tron_address)]` (`app/schemas.py:46-49`).

**Where missing:**
- Most Celery task bodies and Flask view functions are unannotated (`app/tasks.py:43`, `app/api/views.py:21`).
- Many internal helpers use partial typing (`def estimateenergy(src, dst, amount, symbol)` at `app/utils.py:117`).
- No `mypy.ini` or `pyrightconfig.json`; type checking is not enforced.

**Convention for new code:**
- Always annotate function signatures (parameters and return type) when the function has a clear data shape.
- Use `Annotated[...]` types from `app/schemas.py` (`TronAddress`, `TronSymbol`, `KeyType`) instead of bare `str` whenever a value is a Tron address, symbol, or key type, even outside Pydantic.

## Configuration

**Source:**
- Single `Settings(BaseSettings)` class in `app/config.py:13`, instantiated as module-level singleton `config = Settings()` (`app/config.py:169`).
- Pulls from `.env` (loaded via `pydantic_settings.SettingsConfigDict(env_file=".env", extra="ignore")`) and process environment.

**Patterns:**
- Money / blockchain amounts are typed `Decimal`, never `float`: `INTERNAL_TX_FEE: Decimal = Decimal("40")` (`app/config.py:34`). Always pass `Decimal` strings, never floats, when extending settings.
- JSON-encoded structured env vars use `Json[...]` from Pydantic: `MULTISERVER_CONFIG_JSON: Json[List[TronFullnode]] | None = None` (`app/config.py:51`).
- Env-var alias: legacy variable kept via `Field(... alias="BTC_USERNAME")` (`app/config.py:30-31`). Use `alias=` to maintain back-compat when renaming env vars.
- Token contract list is hard-coded inside the `Settings` class (`app/config.py:81-103`). Per-network/per-symbol lookups go through `@cache`-decorated helpers `get_contract_address`, `get_min_transfer_threshold`, `get_symbol`, `get_decimal` (`app/config.py:105-140`).
- Cross-field validation uses `@field_validator(..., mode="after")` with a `@classmethod`: see `validate_external_drain_config_states` (`app/config.py:152-166`).
- `Settings.__hash__` is overridden to a constant so that `@cache` can be applied to instance methods (`app/config.py:149`). This is load-bearing for `get_contract_address`, etc.; keep it when modifying `Settings`.

**Reading config:**
- Always import the singleton: `from .config import config` (relative) or `from app.config import config` (absolute).
- Never instantiate `Settings()` again outside of `app/config.py`.

**Flask config integration:**
- `app.config.from_mapping(config)` (`app/__init__.py:44`) copies pydantic settings into Flask's config dict.
- Custom `AttrConfig(Config)` subclass exposes Flask config keys as attributes (`current_app.config.DATABASE`), see `app/__init__.py:29-39` and usage at `app/db.py:38`.

## Error Handling

**Strategy:**
- Specific custom exceptions live in `app/exceptions.py` (`UnknownTransactionType`, `NotificationFailed`, `BadContractResult`, `AllServersOffline`, `NoServerSet`, `UnknownToken`).
- Encryption-related exceptions live next to their feature: `EncryptionNotSet`, `EncryptionKeyNotSet`, `EncryptionModeMismatch(SystemExit)` (`app/wallet_encryption.py:15-23`). Note `EncryptionModeMismatch` extends `SystemExit` so an unrecoverable mismatch terminates the process.
- Generic `raise Exception(f"...")` is used pervasively for input validation in API views (`app/api/payout.py:27,30,36,40,43,55`). This is the in-tree pattern, even though it is broad; match it for consistency unless adding a new domain exception class.

**Flask error handling:**
- A single blueprint-level handler in `app/api/__init__.py:36-41` catches every `Exception`, logs `traceback.format_exc()` via `logger.warn`, and returns `{"status": "error", "msg": str(e)}` with default 200 status. `HTTPException` instances pass through unchanged.
- Result envelope convention for API responses: `{"status": "success", ...}` on success, `{"status": "error", "msg": str}` on failure. Examples: `app/api/views.py:38-41`, `app/api/staking.py:76-91,124-134`, `app/api/payout.py:90-92`.
- Return shape per route is a `dict`; Flask 3 auto-serializes to JSON. Tuples are used only when overriding the status code, e.g. `return {...}, 401` in `check_credentials` (`app/api/__init__.py:23`).

**Catching specific tronpy errors:**
- `tronpy.exceptions.AddressNotFound`, `tronpy.exceptions.TransactionNotFound`, `tronpy.exceptions.UnknownError`, `tronpy.exceptions.ValidationError`, `tronpy.exceptions.BadKey` are caught at the call sites that depend on them. See `app/wallet.py:46`, `app/api/views.py:77`, `app/tasks.py:594-642`, `app/custom/aml/classes.py:113`. Always catch tronpy-specific exceptions narrowly; never `except:` bare.

**Celery task error handling:**
- Tasks return early with logged warnings instead of raising for known recoverable conditions: `logger.warning(...); return` pattern is dominant (`app/tasks.py:103-106,140-143,178-184,189-193`).
- Per-account loops in long-running scans wrap each iteration in `try/except Exception as e: logger.exception(f"{account} ... error: {e}")` and continue (`app/tasks.py:676-678`, `app/custom/aml/tasks.py:198-199`).
- Background loops (`BlockScanner.__call__`, `block_scanner_stats`, `refresh_best_server_thread_handler`) catch `Exception`, log via `logger.exception`, sleep, and retry forever. Never let a thread die silently; match this pattern when adding new long-running threads.

**Retries:**
- Inline `while ret := 0 < CONCURRENT_MAX_RETRIES:` retry loops inside `scan_accounts` for transient `tronpy.exceptions.UnknownError` (`app/tasks.py:588-602,632-647`).
- `notify_shkeeper` retries forever with `time.sleep(10)` between attempts (`app/tasks.py:524-533`).

## Logging

**Framework:**
- `logging` from stdlib, configured once in `app/logging.py`.
- Always import the named logger: `from .logging import logger` (or `from app.logging import logger`).
- Log format: `"%(levelname)s %(filename)s:%(lineno)s %(threadName)s %(funcName)s(): %(message)s"` (`app/logging.py:6-8`).
- Level: `DEBUG` if `config.DEBUG` else `INFO`. Propagation is disabled (`logger.propagate = False`) so messages are emitted only via the configured `StreamHandler`. Flask's default handler is reformatted to use the same format (`app/logging.py:21-23`).

**When / how to log:**
- `logger.info` for state transitions, successful broadcasts, per-account stats: `app/tasks.py:60`, `app/wallet.py:91`, `app/block_scanner.py:417`.
- `logger.debug` for verbose diagnostics gated behind DEBUG mode (e.g. balance fetch errors, queue contents): `app/tasks.py:595`, `app/block_scanner.py:39`, `app/wallet.py:57`.
- `logger.warning` for non-fatal anomalies and recoverable skip conditions: `app/tasks.py:103-106`, `app/custom/aml/tasks.py:53,103,108`.
- `logger.error` for definite errors that don't raise: `app/utils.py:74`, `app/custom/aml/classes.py:114-117`.
- `logger.exception` for caught exceptions where the traceback is needed: `app/tasks.py:677`, `app/block_scanner.py:66,309,422`, `app/api/staking.py:94`. Always use `logger.exception` (not `logger.error`) inside `except` blocks unless the traceback is irrelevant.
- One legacy spelling: `logger.warn(...)` in `app/api/__init__.py:40`. Prefer `logger.warning` in new code.

**f-strings with `=`:**
- Use the `f"{var=}"` self-documenting form for diagnostic messages: `logger.info(f"{delegetable_sun=} {sun_to_delegate=}")` (`app/tasks.py:137`), `logger.debug(f"{external_drain_list=}")` (`app/custom/aml/classes.py:24`). Prefer this form over manual labeling for debug logs.

## Comments / Docstrings

**Docstrings:**
- Class-level docstrings are uncommon. A handful of functions have prose-style docstrings (`app/api/staking.py:20-29` for `get_staking_info`; `app/tasks.py:90`, `app/tasks.py:556-559` brief one-liners).
- Triple-quoted module docstrings: not used.

**Inline comments:**
- Used heavily to explain blockchain quirks and intent: `app/wallet.py:83-84` (12-hour expiration workaround), `app/db.py:14-30` (NullPool rationale + alternative), `app/block_scanner.py:358-362` (keccak256 of `Transfer(...)` event signature).
- Section headers with `#`-padded banners structure long flow functions: `app/tasks.py:397-399` (`# Same flow for both modes`), `app/tasks.py:579-581` (`# TRC20`), `app/tasks.py:628-630` (`# TRX`).
- TODOs marked inline: `# TODO: implement automatic reward claims` (`app/tasks.py:782`), `# time.sleep(10)  # FIXME` (`app/custom/aml/classes.py:145`).

## Function & Module Design

**Function size:**
- Mixed. Helper functions stay under ~30 lines (most of `app/utils.py`). Workflow orchestrators are intentionally long: `transfer_trc20_from` (`app/tasks.py:88-418`, ~330 lines), `scan_accounts` (`app/tasks.py:553-717`, ~165 lines). When extending these, prefer adding labeled `#` sections rather than refactoring out of band.

**Parameters:**
- Positional with defaults for optional flags: `add_key(type, public=None, uniq_type=True)` (`app/utils.py:45`), `query_db(query, args=(), one=False)` (`app/db.py:54`).
- Keyword arguments for type-annotated optionals: `wallet.transfer(dst, amount, src_address: TronAddress = None)` (`app/wallet.py:61`).

**Return values:**
- API views return plain `dict` (auto-JSONified by Flask), see "Error Handling" above.
- Service / helper functions return rich tuples or pydantic objects: `(PrivateKey | None, str)` in `get_key` (`app/utils.py:66-79`).
- Celery tasks may return dicts, lists, booleans, or `None`; callers usually treat `None`/`False` as a non-fatal skip (`app/tasks.py:106,143,193,222`).

**Exports:**
- No `__all__` declared anywhere. Symbol exposure is implicit via what each module's `__init__.py` imports.
- Blueprints are exposed via `app/api/__init__.py`: it constructs three Flask `Blueprint`s (`api`, `metrics_blueprint`, `staking_bp`) and imports the route modules at the bottom (`app/api/__init__.py:44`) so Flask sees the `@api.post(...)` decorators.
- The Celery instance is constructed in `app/__init__.py:10-18` and reused across modules via `from . import celery` / `from app import celery`.

## Decorators

**Custom decorators:**
- `skip_if_running` in `app/utils.py:133-152`: wraps `@celery.task(bind=True)` tasks and short-circuits if an identical task is already active on a worker. Apply to any periodic / idempotency-sensitive task. Order matters: `@celery.task(bind=True)` must come above `@skip_if_running` so the wrapped function still receives `self`.

**Pattern:**
```python
@celery.task(bind=True)
@skip_if_running
def scan_accounts(self, *args, **kwargs):
    ...
```
(`app/tasks.py:553-555`, `app/tasks.py:720-722`, `app/custom/aml/tasks.py:16-17,27-28,76-77,121-123,139-141`)

**Caching:**
- `@functools.cache` and `@functools.lru_cache(maxsize=...)` on hot lookups: `app/config.py:105,112,126,145`, `app/block_scanner.py:147,154`, `app/api/metrics.py:21`. When adding new hot paths, prefer `@cache` for unbounded but small lookups and `@lru_cache(maxsize=N)` for size-bounded caches.

## Database Access

**Two-track DB access; use the right helper for the right context:**
- **Raw sqlite3 via Flask `g`**: `get_db()`, `query_db(...)` in `app/db.py:35-58`. These rely on Flask's request context (`g`) and the `current_app.config.DATABASE` path. Use them only inside Flask request handlers.
- **Connection-per-call**: `query_db2(...)` in `app/db.py:61-72`. Opens its own sqlite connection each call; safe inside Celery workers, background threads, and class-level definitions. Use this from anywhere outside a Flask request.
- **SQLModel**: `from .db import engine` then `with Session(engine) as session: session.exec(select(...))` for new tables (`Balance`, `Transaction`, `Payout`). All new persistent state should go through SQLModel; see `app/tasks.py:564-621`, `app/custom/aml/classes.py:121-134` for the canonical pattern.

**Connection pooling:**
- The SQLAlchemy engine uses `poolclass=NullPool` (`app/db.py:30`) because Celery forks workers and pooled connections are not fork-safe. Don't change this without auditing every call site.

## Concurrency Patterns

- `concurrent.futures.ThreadPoolExecutor(max_workers=config.CONCURRENT_MAX_WORKERS)` for parallel transfers (`app/tasks.py:78-83`) and block scanning (`app/block_scanner.py:32-34`).
- `threading.Thread(daemon=True, name=..., target=...)` for long-lived background workers started in `run.py` (`run.py:16-49`). Always set `daemon=True` and a descriptive `name=` so the log format's `%(threadName)s` field is meaningful.
- A class-level singleton pattern is used for stateful coordination: `BlockScanner.WATCHED_ACCOUNTS` (`app/block_scanner.py:29`) and `ConnectionManager.instance` (`app/connection_manager.py:18-32`). Prefer this over module-level globals.

---

*Convention analysis: 2026-04-30*

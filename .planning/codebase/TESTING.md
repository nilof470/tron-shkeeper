# Testing Patterns

**Analysis Date:** 2026-04-30

## Test Framework

**Status:** No tests exist in this repository.

- No `tests/` or `test/` directory.
- No `test_*.py` or `*_test.py` files anywhere outside `.venv/`.
- No `conftest.py` files.
- No `pytest`, `unittest`, `nose`, `tox`, or `hypothesis` imports anywhere in `app/`, `run.py`, or `celery_worker.py`.
- No test runner declared in `requirements.txt` (`alembic`, `celery`, `cryptography`, `flask`, `gunicorn`, `prometheus-client`, `pydantic`, `pydantic-settings`, `pymysql`, `redis`, `requests`, `sqlmodel`, `tronpy` only).
- No `pyproject.toml`, `pytest.ini`, `tox.ini`, `setup.cfg`, or any other config that would register a test runner or coverage tooling.
- `.gitignore` and `.dockerignore` both list `.pytest_cache/`, `.coverage`, and `htmlcov/` (`.gitignore:7-9`, `.dockerignore:7-9`), suggesting test tooling was anticipated but not yet adopted.

**Assertion Library:**
- None used.

**Run Commands:**
- None defined. There is no `make test`, no `npm test`-equivalent shell script, no `python -m pytest` invocation in CI.

## CI Configuration

**Workflows present (`.github/workflows/`):**
- `ci.yml` (`.github/workflows/ci.yml`): triggered on `v*.*.*` tags. Builds and pushes a Docker image to Docker Hub via `docker/build-push-action@v4`. No test step.
- `dev-ci.yml` (`.github/workflows/dev-ci.yml`): triggered on every `push`. Builds and pushes a `dev-{{branch}}-{{sha}}` tagged Docker image. No test step.
- `issue_create_auto_reply.yml` (`.github/workflows/issue_create_auto_reply.yml`): unrelated automation for issue replies.

**Implication:** Code is shipped to production based solely on a successful Docker build. There is no automated functional, unit, or integration verification gate.

## Test File Organization (recommended for net-new tests)

When tests are added, follow this layout to mirror the package structure:

```
tron-shkeeper/
├── app/
│   ├── api/
│   │   └── views.py
│   ├── tasks.py
│   ├── wallet.py
│   ├── block_scanner.py
│   ├── connection_manager.py
│   ├── wallet_encryption.py
│   ├── config.py
│   └── custom/
│       └── aml/
│           ├── tasks.py
│           ├── classes.py
│           └── functions.py
└── tests/                              # NEW
    ├── __init__.py
    ├── conftest.py                     # shared fixtures (Flask app, mocked tronpy, sqlite tmp file)
    ├── unit/
    │   ├── test_config.py              # Settings validators, get_contract_address, get_decimal
    │   ├── test_schemas.py             # TronAddress validator, TronTransaction roundtrip
    │   ├── test_wallet.py              # Wallet.balance_of / transfer with mocked Tron client
    │   ├── test_utils.py               # has_free_bw, est_vote_tx_bw_cons, short_txid, skip_if_running
    │   ├── test_block_scanner.py       # parse_tx() — pure function, ideal first target
    │   └── test_wallet_encryption.py   # _encrypt/_decrypt roundtrip, EncryptionModeMismatch
    ├── integration/
    │   ├── test_api_views.py           # Flask test_client against /<symbol>/... endpoints
    │   ├── test_api_payout.py          # multipayout validation, dryrun mode
    │   ├── test_api_staking.py         # /staking/* routes
    │   └── test_db.py                  # query_db / query_db2 / SQLModel session lifecycle
    └── fixtures/
        ├── blocks/                     # captured tronpy block JSON for parse_tx() golden tests
        ├── tx_info/
        └── settings/                   # sample .env-style files for Settings tests
```

**Naming:**
- Test files: `test_<module>.py` (matches `pytest`'s default `python_files`).
- Test functions: `test_<behavior>` for happy-path; `test_<behavior>_when_<condition>` for branching.

**Why this structure:**
- `tests/unit/` mirrors `app/` 1:1, so `app/foo.py` -> `tests/unit/test_foo.py` is a discoverable mapping.
- `tests/integration/` is reserved for tests that boot the Flask app or write to a real (temporary) SQLite database.
- `tests/fixtures/` holds the captured blockchain payloads that `parse_tx`, `add_transaction_to_db`, and the AML pipeline operate on. These are the highest-leverage golden tests this codebase can have.

## Recommended First Tests (in priority order)

The following modules contain pure or easily isolable logic that should be tested first. File paths refer to the production code being tested.

1. **`parse_tx`** in `app/block_scanner.py:315-397` — pure function over a tronpy block dict. Covers `TransferContract` (TRX) and `TriggerSmartContract` (TRC20) branches plus the `BadContractResult`, `UnknownTransactionType`, and `UnknownToken` error paths. Captured fixtures from `tronpy_client.get_block(...)` make this a high-confidence golden test.
2. **`Settings` validators** in `app/config.py:152-166` and helpers `get_contract_address`, `get_min_transfer_threshold`, `get_symbol`, `get_decimal` (`app/config.py:105-140`) — pure logic, no I/O. Verify that `Decimal` defaults round-trip and that `validate_external_drain_config_states` raises when both `aml_check` and `regular_split` are disabled.
3. **`is_tron_address`** validator in `app/schemas.py:38-43` — wraps `tronpy.keys.is_base58check_address`; trivial to test for accept/reject.
4. **`wallet_encryption._encrypt` / `_decrypt`** in `app/wallet_encryption.py:181-188` — round-trip and key-mismatch paths.
5. **`Wallet.balance_of`** in `app/wallet.py:43-53` — mock `self.client` (a `tronpy.Tron` instance) and `self.get_contract()` to verify TRX vs TRC20 branches.
6. **`has_free_bw`** in `app/utils.py:159-174` — thresholds and `use_only_staked` flag, easily testable with a mocked `ConnectionManager.client()`.
7. **`build_payout_list`** in `app/custom/aml/functions.py:132-275` — complex split/AML routing logic that has only ever been exercised in production. Highest payoff for regression safety.

## Mocking (recommended approach)

**Framework:** `pytest-mock` (`mocker.patch`) or stdlib `unittest.mock.patch`. Add either as a dev dependency.

**What to mock:**
- `tronpy.Tron` and its `provider`, `trx`, contract objects. The codebase already isolates Tron access behind `ConnectionManager.client()` (`app/connection_manager.py:27-28`), so patching `app.connection_manager.ConnectionManager.client` is the canonical mock seam.
- HTTP calls: `requests.get` / `requests.post` for the AML provider (`app/custom/aml/functions.py:101-115,123-129`), the Shkeeper backend notifier (`app/block_scanner.py:172-177`, `app/tasks.py:524-533`), and the encryption-key fetch (`app/wallet_encryption.py:79-83`). Use `responses` or `requests_mock` for declarative HTTP fixtures.
- `wallet_encryption.encrypt` / `decrypt`: monkey-patch the class methods to return identity to avoid stubbing PBKDF2HMAC + Fernet inside unit tests.
- `celery.task.delay` / `apply_async`: patch to inspect call args without enqueuing. The `task_always_eager` Celery config option can run tasks synchronously in tests via `celery.conf.task_always_eager = True` in a fixture.

**What NOT to mock:**
- `Decimal` arithmetic, pydantic models, SQLModel sessions against an in-memory `sqlite:///:memory:` engine. Run them for real to catch type and validation regressions.
- The `parse_tx` function itself; feed it captured fixture JSON instead.

**Mocking template:**
```python
def test_balance_of_trx(mocker):
    mock_client = mocker.Mock()
    mock_client.get_account_balance.return_value = Decimal("12.34")
    mocker.patch(
        "app.connection_manager.ConnectionManager.client",
        return_value=mock_client,
    )
    from app.wallet import Wallet
    assert Wallet("TRX").balance_of("Txxx") == Decimal("12.34")
```

## Fixtures and Factories

**Recommended fixture seams:**
- `tests/conftest.py`:
  - `app` fixture: builds a Flask app with an isolated SQLite tempfile via `app.create_app()` (`app/__init__.py:26-86`). Override `config.DATABASE` and `config.DB_URI` with `tmp_path` paths.
  - `client` fixture: returns `app.test_client()` for integration tests; injects HTTP basic auth based on `config.API_USERNAME`/`config.API_PASSWORD` (`app/api/__init__.py:13-23`).
  - `mock_tron` fixture: provides a fully-mocked `tronpy.Tron` returned by `ConnectionManager.client()`.
  - `disable_encryption` fixture: sets `wallet_encryption.encryption = False` so `encrypt`/`decrypt` become no-ops.
  - `eager_celery` fixture: sets `celery.conf.task_always_eager = True` and `celery.conf.task_eager_propagates = True`.
- Captured-fixture JSON under `tests/fixtures/blocks/<block_num>.json` and `tests/fixtures/tx_info/<txid>.json` for `parse_tx` and block-scanner tests.

**Test data:**
- Pydantic factories: instantiate `Token`, `TronTransaction`, `SrVote`, `AmlSplitConfig` directly with literal kwargs. No need for `factory_boy` given the small surface area.

## Coverage

- **Current:** 0%.
- **Recommended target:** 60% for the first iteration (focusing on `parse_tx`, `config.py` helpers, `Wallet`, `wallet_encryption`, AML `build_payout_list`).
- **Tooling:** `pytest --cov=app --cov-report=term-missing --cov-report=html`. Add `pytest`, `pytest-cov`, `pytest-mock`, and `responses` (or `requests-mock`) to a future `requirements-dev.txt`.

## Test Types

**Unit Tests:**
- Suggested scope: pure functions and class methods that can run with all I/O mocked. `app/schemas.py`, `app/config.py`, `app/utils.py`, `app/block_scanner.py:parse_tx`, `app/custom/aml/functions.py`, `app/wallet_encryption.py` are the natural targets.

**Integration Tests:**
- Suggested scope: Flask `test_client()` calls against the registered blueprints (`app/api/__init__.py:8-10`). Use `:memory:` SQLite, monkey-patch `ConnectionManager.client()` to a `Mock`, and assert response envelopes match the `{"status": "...", ...}` convention.

**E2E / Functional Tests:**
- Not feasible without a Tron testnet or recorded VCR-style fixtures of the Nile network. Prefer integration tests with `responses`/`requests-mock` against Tron RPC URLs (`config.FULLNODE_URL`).

## Common Patterns (recommended)

**Async / Celery testing:**
```python
def test_payout_eager(eager_celery, mocker):
    mocker.patch("app.tasks.Wallet.transfer", return_value={
        "status": "success", "txids": ["abc"], "details": {}, "dest": "T...", "amount": "1"
    })
    from app.tasks import payout
    result = payout.delay([{"dst": "T...", "amount": Decimal("1")}], "TRX").get()
    assert result[0]["status"] == "success"
```

**Error testing:**
```python
def test_settings_rejects_disabled_external_drain():
    from app.config import Settings
    with pytest.raises(ValidationError):
        Settings(EXTERNAL_DRAIN_CONFIG='{"aml_check":{"state":"disabled",...},'
                                       '"regular_split":{"state":"disabled",...}}')
```

**Address validation:**
```python
def test_tron_address_rejects_garbage():
    from app.schemas import TronTransaction
    with pytest.raises(ValidationError):
        TronTransaction(status="SUCCESS", txid="x", symbol="TRX",
                        src_addr="not-an-address", dst_addr="T...",
                        amount=Decimal("1"), is_trc20=False)
```

## Suggested CI Additions

Once tests exist, add a workflow at `.github/workflows/test.yml` that runs on every push/PR (before the existing Docker build job in `dev-ci.yml`):

```yaml
name: test
on: [push, pull_request]
jobs:
  pytest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: pytest --cov=app --cov-report=term-missing
```

---

*Testing analysis: 2026-04-30*

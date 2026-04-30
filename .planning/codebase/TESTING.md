# Testing Patterns

**Analysis Date:** 2026-04-30

## Test Framework

**None configured.** Verified:
- No `tests/` directory in source.
- No `test_*.py` or `*_test.py` files.
- No `pytest`, `unittest`, `nose` imports anywhere in `app/`.
- No `pytest.ini`, `pyproject.toml [tool.pytest.ini_options]`, `setup.cfg [tool:pytest]`.
- No CI step calling tests in any GitHub workflow.

## Implications for re:Fee work

- v1 ships without tests. The implementation is small (~150 lines new code) and the existing flow has no tests either; introducing pytest scaffolding *only* for re:Fee would create an awkward inconsistency.
- v2 (later) is a good moment to add a `tests/` directory with `pytest` + `pytest-flask` + `responses` (HTTP mocking) — see recommendation in `shkeeper.io/.planning/codebase/TESTING.md`.
- For Phase 1 (refactor), validation is "run an existing sweep and observe behavior matches" — done as a smoke test, not as automated tests.

## Manual verification approach (used during phases)

- **Phase 1:** start sidecar with existing `ENERGY_DELEGATION_MODE=1` config; trigger a real or simulated sweep via celery shell; check logs for the same INFO-level narrative as before refactor.
- **Phase 2:** start sidecar with `ENERGY_SOURCE=refee`, `REFEE_API_KEY=<test key>`; check that:
  - With key set: `acquire` makes the POST, polls, returns True.
  - Without key set: pydantic raises at process startup.
- **Phase 3:** end-to-end on TRON mainnet with a real test transaction.

## Recommended future tests (out of scope for v1)

If/when adding pytest:

1. **Unit:** `EnergyProvider.acquire/release` for both providers, with tronpy and `requests` mocked.
2. **Integration:** `transfer_trc20_from` happy path with a fake celery context and tronpy stub; verify `provider.acquire` is called with correct args.
3. **Settings validator:** `ENERGY_SOURCE=refee` without `REFEE_API_KEY` raises; with key passes.
4. **Polling:** terminal status (failed, insufficient_funds, canceled) results in False; timeout case results in False.
5. **On-chain double-check:** if re:Fee says delegated but `EnergyLimit` is too low, `acquire` returns False.

## Run Commands

```bash
# Currently: nothing.

# Future (post-v2):
pytest tests/                 # all
pytest tests/ -k refee        # subset
pytest tests/ --cov=app       # coverage
```

---

*Testing analysis: 2026-04-30*

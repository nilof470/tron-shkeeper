# Codebase Concerns

**Analysis Date:** 2026-04-30
**Source:** spike 002 recon + targeted reads.

## Tech Debt

### `transfer_trc20_from` is too long with nested helpers

- Issue: ~280-line celery task with two inline nested functions (`calc_sun_for_energy_delegation`, `delegate_energy`).
- Files: `app/tasks.py:88-418`.
- Impact: hard to test, hard to read, mixes concerns (activation, bandwidth, energy estimate, delegation, transfer, undelegate cleanup).
- Fix approach: **Phase 1 of this project** lifts the energy concerns out into `app/energy_provider.py`. Reduces `transfer_trc20_from` by ~80 lines and makes Phase 2 a small diff.

### No tests at all

- Issue: zero test files, zero pytest config, zero CI test step.
- Files: project-wide.
- Impact: refactors and integrations are validated only by manual smoke tests; regressions are detected by operators in production.
- Fix approach: out of scope for v1 of this project. Pytest scaffolding recommended for v2 — see `codebase/TESTING.md`.

### No formatter / linter / type checker

- Issue: no `black`, `ruff`, `flake8`, `mypy`, etc.
- Files: project-wide.
- Impact: minor — codebase style is mostly consistent organically; no enforcement.
- Fix approach: out of scope. If introduced, do it in a single dedicated PR to avoid mixing reformat noise with feature work.

### Type hints sparse

- Issue: type hints exist in newer code (e.g. `app/utils.py:100`) but are missing from older paths.
- Files: `app/tasks.py`, parts of `app/wallet.py`.
- Impact: reduced IDE support; harder for newcomers.
- Fix approach: add type hints in **new** code (Phase 1 / Phase 2). Don't retrofit older files in this project.

## Known Bugs / Quirks

- The merchant-payout endpoint URL doesn't carry source address; source is implicit (= fee_deposit). Operators reading the URL might assume otherwise. Not a bug, but a documentation gap.
- TRX-burn-mode payout (`else` branch at `app/tasks.py:366-395`) sends `INTERNAL_TX_FEE` (40 TRX) from main → onetime as a static budget, not based on actual energy needed. Over-pays in many cases. Not addressed in this project.

## Security

### `SHKEEPER_BACKEND_KEY` default = `"shkeeper"`

- Risk: known-default header value used to authenticate decrypt requests from sidecar to main. If exposed beyond k8s cluster network, attacker can fetch wallet decryption key.
- Files: `app/config.py:32` (`SHKEEPER_BACKEND_KEY: str = "shkeeper"`); upstream main repo also uses the literal default at `shkeeper/api_v1.py`.
- Mitigation in current architecture: cluster-internal network isolates the call.
- Recommendation: out of scope here, but operators should override `SHKEEPER_BACKEND_KEY` in their helm values. Track separately in companion repo's `CONCERNS.md`.

### re:Fee API key handling

- Risk: leaking `REFEE_API_KEY` if logged or echoed in errors.
- Files: `app/config.py` (Phase 2 — will add `REFEE_API_KEY: SecretStr | None = None`).
- Mitigation: pydantic `SecretStr` redacts in repr/str; only call `.get_secret_value()` when actually constructing the HTTP header. Don't log the key.
- Verification: code review during Phase 2 must check no log statement formats `REFEE_API_KEY` into a string.

### IP whitelist on re:Fee side

- Risk: re:Fee API has IP-whitelist support (`whitelisted_ips` in user profile). If empty, requests come from any IP — fine for ourselves, but if API key leaks and re:Fee account isn't whitelist-protected, the leak is more impactful.
- Recommendation: operator-side hardening. Add a note in operator docs (Phase 3) recommending whitelist of cluster egress IP.

## Performance / Scaling

### Synchronous polling in celery worker

- Issue: `RefeeEnergyProvider.acquire` will sleep up to 60s during polling, occupying the celery worker.
- Files: `app/tasks.py` after Phase 2 wiring.
- Mitigation: celery handles concurrency natively; multiple sweeps can run in parallel workers. The 60s window is per-sweep and bounded.
- Concern: if many user-wallets cross the threshold simultaneously and re:Fee is slow, worker pool fills up. Match worker count to expected concurrent sweeps.

### Hardcoded `BALANCES_RESCAN_PERIOD` = 3600s

- Issue: scan cadence is global, not adaptive. High-throughput merchants might want shorter cadence.
- Out of scope but documented for v2.

## Fragile Areas

### Inline `delegate_energy` re-checks resource state via fresh RPC

- Files: `app/tasks.py:168-176`.
- Why fragile: a stale RPC response right after broadcast can cause false-negative ("delegated but I don't see it yet"). Mitigated by `signed_tx.broadcast().wait()` which waits for chain confirmation.
- Phase 2 carries the same pattern — `RefeeEnergyProvider.acquire` does the same on-chain double-check after re:Fee says `delegated`.

## Test Coverage Gaps

- Sweep flow: completely untested. High risk because money moves.
- Settings validation: untested.
- Concurrency under retry: untested.

Priority for re:Fee project: **High** for Phase 2 happy-path validation (manual). **Low** for adding pytest infra in v1.

---

*Concerns audit: 2026-04-30*

---
phase: 03-live-e2e-validation-docs
plan: 01
subsystem: live-e2e-prep
tags: [refee, e2e, runbook]
key-files:
  created:
    - .planning/phases/03-live-e2e-validation-docs/03-LIVE-E2E-RUNBOOK.md
    - .planning/phases/03-live-e2e-validation-docs/phase3_refee_e2e.py
completed: 2026-04-30
---

# Phase 3 Plan 01: Live E2E Preparation Summary

Prepared the local Phase 3 live sweep workflow without changing production
application code.

## What Changed

- Added a runbook for the controlled re:Fee USDT sweep test.
- Added `phase3_refee_e2e.py`, a helper that:
  - uses an isolated default DB under `/tmp/tron-shkeeper-phase3-e2e`;
  - builds `REFEE` config from `/tmp/refee-live.env` when needed;
  - generates a sidecar-controlled onetime wallet;
  - checks USDT/TRX/account-resource readiness;
  - can run `transfer_trc20_from(..., "USDT")` after explicit `--yes`.

## Generated Test Wallet

- Onetime address to fund: `TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7`
- Fee-deposit destination: `TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k`
- Test DB: `/tmp/tron-shkeeper-phase3-e2e/database.db`

Initial readiness check:

- `private_key_present`: true
- `account_active`: false
- `trc20_balance`: `0`
- `trx_balance`: `0`
- `ready_for_clean_sweep`: false

## Verification

- Initial RED check confirmed the helper did not exist before creation.
- `py_compile` passed for `phase3_refee_e2e.py`.
- `phase3_refee_e2e.py self-test` printed `self-test: OK`.
- `phase3_refee_e2e.py show-env` confirmed re:Fee mode without printing the API key.
- `generate-wallet` created the onetime wallet and fee-deposit account in the
  isolated Phase 3 DB.
- `check-wallet` confirmed the generated onetime private key is present.

## Next

Send `6-10 USDT-TRC20` to `TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7`.
Do not send TRX. After the deposit confirms, rerun `check-wallet`; the expected
ready state is `account_active=true`, `trc20_balance > 5`, `trx_balance=0`, and
`ready_for_clean_sweep=true`.

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

## Live E2E Result

The live Phase 3 sweep succeeded on 2026-04-30.

- Onetime source: `TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7`
- Fee-deposit destination: `TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k`
- Pre-sweep onetime balance: `3 USDT`, `0 TRX`
- Threshold: `1 USDT`
- re:Fee order: `53b09ed3-ef17-4798-bc07-539d39e5c95d`
- re:Fee delegation tx: `7e034daae3e0c2ab20c36602740b5a21386792765ac62e79763ba5c3031ab740`
- Energy delegated: `145350`
- Energy required by transfer estimate: `138428`
- Successful USDT transfer tx: `9bdfabfee0c57508c0a58d1521c6f512ecb07f54eff219a8f56cf81f3b10634f`
- Successful transfer receipt: `SUCCESS`
- Transfer resource usage: `130285` energy, `345` bandwidth
- Post-sweep onetime balance: `0 USDT`, `0 TRX`
- Post-sweep fee-deposit balance check: `3 USDT`, `4.900002 TRX`
- Sweep report: `/tmp/tron-shkeeper-phase3-e2e/phase3-refee-sweep-20260430T154001Z.json`

Important finding: re:Fee covers energy, but the onetime sender still needs
enough bandwidth for the TRC20 transaction. A first sweep attempt rented energy
successfully but stopped before broadcasting the transfer because available
bandwidth was `334` and the app requires `346`. After bandwidth was delegated to
the onetime address (`staked_bw=999`), the sweep succeeded without sending TRX
to the onetime wallet during the successful sweep (`tx_trx_res: null`).

## Next

Convert the Phase 3 runbook findings into production operator documentation:
account activation and bandwidth are separate prerequisites from re:Fee energy
rental.

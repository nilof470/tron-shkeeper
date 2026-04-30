# Phase 3 Live re:Fee Sweep Runbook

## Purpose

Verify the full sidecar flow on mainnet:

1. a user-wallet controlled by `tron-shkeeper` receives USDT-TRC20;
2. the wallet has zero TRX;
3. `transfer_trc20_from` rents energy through re:Fee;
4. the USDT sweep to the fee-deposit account succeeds;
5. no TRX is burned from the user-wallet.

## Safety Model

- Use a fresh local test DB under `/tmp/tron-shkeeper-phase3-e2e` by default.
- Generate the user-wallet through this sidecar so its private key is present in
  the local `keys` table.
- Do not send TRX to the user-wallet for the clean test.
- Send only a small USDT-TRC20 amount above the configured threshold, for example
  `6-10 USDT`.
- Keep `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT=false` so a re:Fee
  failure does not silently turn into a TRX-burn success.

## Prerequisites

- `/tmp/refee-live.env` exists and contains:

```bash
export REFEE_API_KEY="..."
export REFEE_RENT_DURATION_LABEL=1h
```

- re:Fee balance is topped up.
- The machine can reach the configured TRON fullnode.

## Commands

Run commands from `/Users/test/PycharmProjects/tron-shkeeper`.

### 1. Check helper setup

```bash
zsh -lc 'set -a; source /tmp/refee-live.env; set +a; \
  /tmp/tron-shkeeper-py312-venv/bin/python \
  .planning/phases/03-live-e2e-validation-docs/phase3_refee_e2e.py show-env'
```

Expected:

- `energy_source` is `refee`;
- `refee_api_key_present` is `true`;
- `burn_fallback` is `false`;
- database paths are under `/tmp/tron-shkeeper-phase3-e2e`.

### 2. Generate a controlled user-wallet

```bash
zsh -lc 'set -a; source /tmp/refee-live.env; set +a; \
  /tmp/tron-shkeeper-py312-venv/bin/python \
  .planning/phases/03-live-e2e-validation-docs/phase3_refee_e2e.py generate-wallet'
```

Save the printed `onetime_address`.

### 3. Fund the generated user-wallet

From an external wallet, send `6-10 USDT-TRC20` to `onetime_address`.

Do not send TRX to the user-wallet. The USDT deposit should activate the account
on-chain while keeping TRX balance at zero.

### 4. Check readiness

```bash
ADDR="T_GENERATED_ONETIME_ADDRESS"
zsh -lc "set -a; source /tmp/refee-live.env; set +a; \
  /tmp/tron-shkeeper-py312-venv/bin/python \
  .planning/phases/03-live-e2e-validation-docs/phase3_refee_e2e.py \
  check-wallet --address $ADDR"
```

Required before sweep:

- `private_key_present: true`
- `account_active: true`
- `trc20_balance` greater than `min_transfer_threshold`
- `trx_balance: "0"`
- `ready_for_clean_sweep: true`

If `account_active` is false, wait for the USDT deposit to finalize and check
again. Do not run sweep while inactive, because the sidecar activation branch can
send TRX to the user-wallet.

### 5. Run the live sweep

```bash
ADDR="T_GENERATED_ONETIME_ADDRESS"
zsh -lc "set -a; source /tmp/refee-live.env; set +a; \
  /tmp/tron-shkeeper-py312-venv/bin/python \
  .planning/phases/03-live-e2e-validation-docs/phase3_refee_e2e.py \
  run-sweep --address $ADDR --yes"
```

The helper writes a JSON report under:

```text
/tmp/tron-shkeeper-phase3-e2e/
```

## Evidence To Record

- generated `onetime_address`;
- fee-deposit destination address;
- pre-sweep USDT/TRX balance;
- re:Fee order id/status/txn hash from logs;
- USDT transfer tx hash from `transfer_result`;
- post-sweep USDT/TRX balance;
- confirmation that user-wallet TRX stayed zero.

## Failure Handling

- If re:Fee order fails, do not enable burn fallback for this clean test. Record
  the failed status/body and stop.
- If `trx_balance` is non-zero, either restart with a fresh generated wallet or
  run with `--allow-nonzero-trx` only if the goal is functional sweep testing
  rather than zero-TRX proof.
- If account activation is needed, do not let the sidecar activation branch run
  for the clean zero-TRX proof.

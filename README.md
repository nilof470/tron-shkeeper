# tron-shkeeper

TRON sidecar service for SHKeeper. It watches generated one-time TRON
addresses, reports deposits, and sweeps TRX/TRC20 balances back to the
fee-deposit wallet.

## Runtime

The application is configured through environment variables. Defaults live in
`app/config.py`.

Core services:

- Flask app: `run.py`
- Celery worker app: `celery_worker.py`
- Redis broker/backend: `REDIS_HOST`
- SQLite databases: `DATABASE`, `DB_URI`, `BALANCES_DATABASE`
- TRON fullnode: `FULLNODE_URL` or `MULTISERVER_CONFIG_JSON`

Typical local commands:

```bash
python run.py
celery -A celery_worker.celery worker -E --loglevel=info
```

## Energy Source

TRC20 sweeps need TRON energy. The sidecar supports two energy sources:

```bash
ENERGY_SOURCE=staking  # default
ENERGY_SOURCE=refee
```

`staking` preserves the legacy freeze/delegate resource flow.

`refee` rents energy from re:Fee per sweep through
`POST /api/rent_resource/orders`, waits for `status="delegated"`, verifies the
on-chain energy on the onetime address, then broadcasts the TRC20 transfer.

## re:Fee Setup

Set `ENERGY_SOURCE=refee` and provide `REFEE` as JSON:

```bash
export ENERGY_SOURCE=refee
export REFEE='{"api_key":"YOUR_REFEE_API_KEY","rent_duration_label":"1h"}'
```

Optional `REFEE` fields:

```json
{
  "api_base_url": "https://api.refee.bot/v2",
  "api_key": "YOUR_REFEE_API_KEY",
  "rent_duration_label": "1h",
  "energy_overprovision_factor": "1.05",
  "poll_interval_sec": 2.0,
  "timeout_sec": 60
}
```

Allowed `rent_duration_label` values are `1h`, `1d`, `3d`, `7d`, and `14d`.
`api_key` is required and must be non-empty. When `ENERGY_SOURCE=refee`,
startup fails if `REFEE` is missing.

## Sweep Prerequisites

re:Fee rents **energy only**. A TRC20 sweep still needs bandwidth from the
onetime sender address.

Before creating a re:Fee order, `transfer_trc20_from` checks:

- token balance is above the token threshold, for example
  `USDT_MIN_TRANSFER_THRESHOLD`;
- the onetime account is active on-chain;
- the onetime account has enough free or delegated bandwidth for the TRC20
  transaction.

If bandwidth is not available, the sweep stops before renting re:Fee energy.
The USDT remains on the onetime address and the next block-scanner or periodic
balance scan can retry after bandwidth recovers or is delegated manually.

The default periodic rescan interval is:

```bash
BALANCES_RESCAN_PERIOD=3600
```

## Account Activation

If the onetime address is not active on-chain, the sidecar activates it by
sending `0.1 TRX` from the `fee_deposit` wallet to the onetime wallet. This is
separate from re:Fee.

The activation branch requires:

- `fee_deposit` has at least `1.1 TRX`;
- `fee_deposit` has enough staked bandwidth for the activation transfer, unless
  `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH=true`.

After activation, the onetime account normally receives daily free bandwidth.
That bandwidth can be used for the TRC20 transfer while re:Fee supplies energy.

## Burn Fallback Flags

For a strict re:Fee-only payout path, keep:

```bash
ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT=false
ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH=false
```

With `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT=false`, a failed re:Fee
energy acquire does not fall back to funding the onetime wallet for a TRX-burn
TRC20 transfer.

With `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH=false`, bandwidth burn
is not allowed in resource-management checks. Operators should wait for free
bandwidth to recover or delegate/rent bandwidth to the specific address.

## Helm / Container Configuration

This repository does not include a Helm chart. In chart-based deployments, pass
the same environment variables to the tron-shkeeper sidecar container. Example
shape for a `values.yaml` override:

```yaml
env:
  ENERGY_SOURCE: refee
  REFEE: '{"api_key":"YOUR_REFEE_API_KEY","rent_duration_label":"1h"}'
  ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT: "false"
  ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH: "false"
```

If the chart models environment variables as a list, use the same keys:

```yaml
env:
  - name: ENERGY_SOURCE
    value: refee
  - name: REFEE
    value: '{"api_key":"YOUR_REFEE_API_KEY","rent_duration_label":"1h"}'
  - name: ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT
    value: "false"
  - name: ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH
    value: "false"
```

## Live re:Fee Validation

The re:Fee integration was validated on TRON mainnet on 2026-04-30:

- source onetime: `TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7`
- destination fee-deposit: `TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k`
- swept amount: `3 USDT`
- re:Fee delegated energy: `145350`
- USDT transfer energy used: `130285`
- USDT transfer bandwidth used: `345`
- transfer tx:
  `9bdfabfee0c57508c0a58d1521c6f512ecb07f54eff219a8f56cf81f3b10634f`

The onetime address ended with `0 USDT` and `0 TRX`.

## Tests

Run the test suite with:

```bash
python -m unittest discover -s tests
```

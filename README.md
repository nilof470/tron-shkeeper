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
gunicorn run:server
celery -A celery_worker.celery worker -E --loglevel=info
```

When `TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED=true`, run a dedicated
single-slot worker for USDT payout resource provisioning in addition to the
normal worker:

```bash
celery -A celery_worker.celery worker -E --loglevel=info -Q celery
celery -A celery_worker.celery worker -E --loglevel=info \
  -Q tron_usdt_fee_payouts --concurrency=1 --prefetch-multiplier=1 \
  -n tron-usdt-payouts@%h
```

## Resource Providers

TRON USDT payouts and sweeps use the same external resource flow when
`ENERGY_PROVIDER` or `BANDWIDTH_PROVIDER` is `profeex` or `refee`:

1. Estimate required USDT transfer energy.
2. Check bandwidth on the sender/source address.
3. Rent bandwidth only if the source address is short.
4. Check energy on the sender/source address.
5. Rent energy only if the source address is short.
6. Re-check on-chain resources.
7. Broadcast the USDT transfer only when resources are ready.

For payout, the source is `fee_deposit` and the destination is the customer
wallet. For sweep, the source is the onetime/client wallet and the destination
is `fee_deposit`.

Provider selectors:

```bash
ENERGY_PROVIDER=staking       # default
BANDWIDTH_PROVIDER=disabled   # default
ENERGY_PROVIDER=refee
BANDWIDTH_PROVIDER=refee
ENERGY_PROVIDER=profeex
BANDWIDTH_PROVIDER=profeex
TRON_USDT_RESOURCE_FALLBACK_PROVIDER=refee
```

`staking` preserves the legacy freeze/delegate resource flow when
`ENERGY_DELEGATION_MODE=true`. With the default `ENERGY_DELEGATION_MODE=false`,
the sidecar uses the legacy TRX burn funding flow.

`TRON_USDT_RESOURCE_FALLBACK_PROVIDER=refee` is the ProfeeX-primary fallback for
USDT payout and USDT sweep. Use this production shape:

```bash
ENERGY_PROVIDER=profeex
BANDWIDTH_PROVIDER=profeex
TRON_USDT_RESOURCE_FALLBACK_PROVIDER=refee
```

Fallback is only safe before a provider order is accepted or ambiguously
accepted. Network errors, timeouts, HTTP 408/429/5xx, rate limiting, temporary
provider failures, insufficient provider balance, and malformed responses before
order acceptance can fallback from ProfeeX to re:Fee. Once ProfeeX or re:Fee has
accepted an order/task, or returned an accepted-looking response without a
usable id, the transfer stops before broadcast and does not switch providers.
This avoids double-renting resources for the same transfer.

`BANDWIDTH_PROVIDER` controls bandwidth rental independently. Allowed values are
`disabled`, `refee`, and `profeex`.

## re:Fee Setup

For direct re:Fee primary mode, set `ENERGY_PROVIDER=refee`. For ProfeeX
fallback mode, keep `ENERGY_PROVIDER=profeex` and set
`TRON_USDT_RESOURCE_FALLBACK_PROVIDER=refee`. In both cases provide `REFEE` as
JSON:

```bash
export ENERGY_PROVIDER=refee
export BANDWIDTH_PROVIDER=refee
export REFEE='{"api_key":"YOUR_REFEE_API_KEY","rent_duration_label":"1h"}'
export REFEE_FIXED_ENERGY_ORDER_AMOUNT=65000
```

Optional `REFEE` fields:

```json
{
  "api_base_url": "https://api.refee.bot/v2",
  "api_key": "YOUR_REFEE_API_KEY",
  "rent_duration_label": "1h",
  "bandwidth_rent_duration_label": "1h",
  "energy_overprovision_factor": "1.05",
  "min_energy_order_amount": 30000,
  "min_bandwidth_order_amount": 1000,
  "poll_interval_sec": 2.0,
  "timeout_sec": 60
}
```

Allowed `rent_duration_label` values are `1h`, `1d`, `3d`, `7d`, and `14d`.
`api_key` is required and must be non-empty. Startup fails if `REFEE` is missing
while re:Fee is selected as energy provider, bandwidth provider, or USDT
resource fallback provider.

`min_energy_order_amount` defaults to `30000`, matching the live re:Fee energy
order minimum observed from the API. `REFEE_FIXED_ENERGY_ORDER_AMOUNT` defaults
to `65000` and acts as an order default/lower bound inside the re:Fee provider.
It is not the USDT payout/sweep energy estimate. Shared USDT resource
provisioning uses the provider estimate chain, and strict provisioning can
request more than `REFEE_FIXED_ENERGY_ORDER_AMOUNT` when the estimate requires
more energy. Fixed values must be `0` or greater than or equal to
`min_energy_order_amount`.

re:Fee USDT estimates use `GET /api/functions/cost/{source_address}`. re:Fee
bandwidth orders keep the provider minimum of `1000`; callers request the actual
transfer requirement, normally `346`, and the provider submits at least `1000`.

## ProfeeX Setup

Set `ENERGY_PROVIDER=profeex` and/or `BANDWIDTH_PROVIDER=profeex`, then provide
`PROFEEX` as JSON. For the production primary/fallback flow, also configure
re:Fee:

```bash
export ENERGY_PROVIDER=profeex
export BANDWIDTH_PROVIDER=profeex
export TRON_USDT_RESOURCE_FALLBACK_PROVIDER=refee
export PROFEEX='{"api_key":"YOUR_PROFEEX_API_KEY","energy_duration_label":"1h","bandwidth_duration_label":"1h","currency":"TRX","fixed_energy_order_amount":65000,"fixed_bandwidth_order_amount":350}'
export REFEE='{"api_key":"YOUR_REFEE_API_KEY","rent_duration_label":"1h"}'
```

Optional `PROFEEX` fields:

```json
{
  "api_base_url": "https://api.profeex.io/api/v1",
  "api_key": "YOUR_PROFEEX_API_KEY",
  "currency": "TRX",
  "energy_duration_label": "1h",
  "bandwidth_duration_label": "1h",
  "fixed_energy_order_amount": 65000,
  "fixed_bandwidth_order_amount": 350,
  "poll_interval_sec": 2.0,
  "timeout_sec": 60
}
```

Allowed duration values are `1h`, `1d`, `3d`, `7d`, and `14d`.
`api_key` is required and must be non-empty. Startup fails if `PROFEEX` is
missing while either `ENERGY_PROVIDER=profeex` or `BANDWIDTH_PROVIDER=profeex`.

`fixed_energy_order_amount` defaults to `65000`; the sidecar treats `64500`
available energy as enough to avoid duplicate fixed rentals when no higher
strict estimate is required. Shared USDT resource provisioning can request a
higher ProfeeX order amount when the current estimate and source deficit require
it.
`fixed_bandwidth_order_amount` defaults to `350`, which is the normal TRC20
transfer bandwidth order size used before energy provisioning.

## Sweep Prerequisites

A TRC20 sweep needs bandwidth from the onetime sender address before energy is
provisioned. `transfer_trc20_from` checks:

- token balance is above the token threshold, for example
  `USDT_MIN_TRANSFER_THRESHOLD`;
- the onetime account is active on-chain;
- the onetime account has enough free or delegated bandwidth for the TRC20
  transaction.

If bandwidth is not currently available:

- `BANDWIDTH_PROVIDER=disabled` uses only wallet bandwidth that is already
  available. The sweep stops before energy provisioning, leaving funds on the
  onetime address for the next block-scanner or periodic balance scan to retry
  after TRON daily bandwidth recovery or manual delegation.
- `BANDWIDTH_PROVIDER=refee` rents re:Fee bandwidth before energy provisioning,
  then continues to energy provisioning after bandwidth is available.
- `BANDWIDTH_PROVIDER=profeex` rents ordinary ProfeeX bandwidth for the onetime
  wallet before energy provisioning, then continues after bandwidth is
  available. If ProfeeX fails before an accepted/ambiguous order and
  `TRON_USDT_RESOURCE_FALLBACK_PROVIDER=refee`, the sweep tries re:Fee bandwidth
  on the same onetime source address. It does not call a provider when the wallet
  already has enough bandwidth.

The default periodic rescan interval is:

```bash
BALANCES_RESCAN_PERIOD=3600
```

## Account Activation

If a sweep onetime address is not active on-chain, the sidecar activates it by
sending `0.1 TRX` from the `fee_deposit` wallet to the onetime wallet.

The activation branch requires:

- `fee_deposit` has at least `1.1 TRX`;
- `fee_deposit` has enough staked bandwidth for the activation transfer, unless
  `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH=true`.

After activation, the onetime account normally receives daily free bandwidth.
That bandwidth can be used for the TRC20 transfer while ProfeeX or re:Fee
supplies energy.

For payout destination activation, ProfeeX remains primary. When
`TRON_USDT_RESOURCE_FALLBACK_PROVIDER=refee`, operational ProfeeX activation
failures can fallback to re:Fee activation through
`POST /api/functions/activate?address=<destination>`. A re:Fee energy estimate
does not prove the payout destination is active, so activation status is checked
through TRON account state or the activation provider chain before transfer
resources are rented.

## Burn Fallback Flags

To avoid legacy TRX-burn fallback in external-provider USDT flows, keep:

```bash
ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT=false
ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH=false
```

With `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT=false`, a failed external
provider energy acquire does not fall back to funding the onetime wallet for a
TRX-burn TRC20 transfer.

With `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH=false`, bandwidth burn
is not allowed in resource-management checks. Operators should wait for free
bandwidth to recover or delegate/rent bandwidth to the specific address.

## Helm / Container Configuration

This repository does not include a Helm chart. In chart-based deployments, pass
the same environment variables to the tron-shkeeper sidecar container. Example
shape for a `values.yaml` override:

```yaml
env:
  ENERGY_PROVIDER: profeex
  BANDWIDTH_PROVIDER: profeex
  TRON_USDT_RESOURCE_FALLBACK_PROVIDER: refee
  PROFEEX: '{"api_key":"YOUR_PROFEEX_API_KEY","energy_duration_label":"1h","bandwidth_duration_label":"1h","currency":"TRX","fixed_energy_order_amount":65000,"fixed_bandwidth_order_amount":350}'
  REFEE: '{"api_key":"YOUR_REFEE_API_KEY","rent_duration_label":"1h"}'
  REFEE_FIXED_ENERGY_ORDER_AMOUNT: "65000"
  ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT: "false"
  ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH: "false"
```

If the chart models environment variables as a list, use the same keys:

```yaml
env:
  - name: ENERGY_PROVIDER
    value: profeex
  - name: BANDWIDTH_PROVIDER
    value: profeex
  - name: TRON_USDT_RESOURCE_FALLBACK_PROVIDER
    value: refee
  - name: PROFEEX
    value: '{"api_key":"YOUR_PROFEEX_API_KEY","energy_duration_label":"1h","bandwidth_duration_label":"1h","currency":"TRX","fixed_energy_order_amount":65000,"fixed_bandwidth_order_amount":350}'
  - name: REFEE
    value: '{"api_key":"YOUR_REFEE_API_KEY","rent_duration_label":"1h"}'
  - name: REFEE_FIXED_ENERGY_ORDER_AMOUNT
    value: "65000"
  - name: ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT
    value: "false"
  - name: ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH
    value: "false"
```

For the full k3s/Helm deployment runbook used with the private GHCR image and
re:Fee configuration, see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

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

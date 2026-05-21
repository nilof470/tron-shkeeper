# ProfeeX Energy Provider Design

Date: 2026-05-21

## Goal

Add ProfeeX as an energy rental provider for TRC20 sweep flows. Bandwidth rental through ProfeeX already exists and must keep working. The selected energy provider must continue to be controlled by configuration.

## Current Context

The project already has provider interfaces for energy and bandwidth:

- `EnergyProvider.acquire_energy(...)`
- `BandwidthProvider.acquire_bandwidth(...)`

`ENERGY_PROVIDER` currently supports `staking` and `refee`. `BANDWIDTH_PROVIDER` supports `disabled`, `refee`, and `profeex`. The ProfeeX API integration currently handles bandwidth through `/delegation/buybandwidth` and polls `/delegation/status/{task_id}`.

ProfeeX documentation defines energy rental through:

- `POST /api/v1/delegation/buyenergy`
- `GET /api/v1/delegation/status/{task_id}`

The energy volume accepted by the OpenAPI spec is `64285..3000000`.

## Configuration

`ENERGY_PROVIDER` will support `profeex`:

```yaml
ENERGY_PROVIDER: "profeex"
BANDWIDTH_PROVIDER: "profeex"
PROFEEX: '{"api_key":"...","energy_duration_label":"1h","bandwidth_duration_label":"1h","currency":"TRX","fixed_energy_order_amount":65000,"fixed_bandwidth_order_amount":350}'
```

`PROFEEX` will include resource-specific fixed order sizes:

- `fixed_energy_order_amount`: default `65000`
- `fixed_bandwidth_order_amount`: default `350`
- `energy_duration_label`: default `1h`
- `bandwidth_duration_label`: default `1h`
- `currency`: default `TRX`

Provider API limits become internal constants and validation guards, not JSON config fields:

- energy minimum `64285`
- energy maximum `3000000`
- bandwidth minimum `350`
- bandwidth maximum `10000`

Existing ProfeeX min/max bandwidth config fields should be removed from the deployment-facing model because backward compatibility is not required for this feature branch. The deployment knobs are the fixed order amounts. The provider limits exist so the app can reject impossible ProfeeX orders before sending an API request.

## Architecture

The existing ProfeeX provider will become a dual-capability provider that implements both `EnergyProvider` and `BandwidthProvider`.

`factory.get_energy_provider()` will return the ProfeeX provider when `ENERGY_PROVIDER == "profeex"`.

`factory.get_bandwidth_provider()` will keep returning the same ProfeeX provider when `BANDWIDTH_PROVIDER == "profeex"`.

Keep the existing class name `ProfeeXBandwidthProvider` as a compatibility alias inside the codebase. The implementation should be generalized enough that energy and bandwidth share request/polling helpers instead of duplicating API mechanics.

ProfeeX must implement `release_energy(receiver)` as a no-op with a log message. The sweep code calls `release_energy()` after successful provider-mode transfers, while ProfeeX rented resources expire on the provider side.

Staking energy remains separate because it uses local delegation from the configured energy account. re:Fee and ProfeeX are both external providers, but their provider-specific API details stay inside their provider classes.

## Energy Flow

Before ordering energy, the provider checks current on-chain available energy for the target wallet.

For ProfeeX fixed energy mode:

```text
energy_threshold = fixed_energy_order_amount - 500

if available_energy >= energy_threshold:
  skip rental
else:
  create ProfeeX buyenergy order with volume=fixed_energy_order_amount
  poll until ACTIVE, failure, or timeout
  verify available_energy >= energy_threshold after ACTIVE
```

The `500` tolerance mirrors the existing re:Fee fix where providers may delegate slightly less than the requested fixed amount on-chain. With the default `fixed_energy_order_amount=65000`, `64500` available energy is considered enough.

The app must not use the existing `get_estimated_energy()` value as the fixed ProfeeX order size because it is known to overestimate around `72000` while the real USDT sweep usually consumes about `65000`.

`tasks.py` may still call `get_estimated_energy()` to decide that the sweep needs provider-mode handling, but ProfeeX must not use that estimate as the order volume. ProfeeX's own pre-order and post-order checks use the fixed threshold above.

The `64500` threshold is an operational tolerance, not a guarantee that TRON will never burn a small amount if actual usage spikes above available energy. The default is accepted because observed USDT sweep usage is around `64285-65000`; if this changes, operators should raise `fixed_energy_order_amount`.

## Bandwidth Flow

Bandwidth keeps the same high-level behavior, but order sizing becomes explicitly fixed for ProfeeX:

```text
if has_free_bw(wallet, required_bandwidth):
  skip rental
else:
  create ProfeeX buybandwidth order with volume=fixed_bandwidth_order_amount
  poll until ACTIVE, failure, or timeout
  verify has_free_bw(wallet, required_bandwidth) after ACTIVE
```

The default `fixed_bandwidth_order_amount=350` matches the minimum ProfeeX bandwidth order and the expected TRC20 transfer bandwidth requirement.

If `fixed_bandwidth_order_amount < required_bandwidth`, the provider must fail before creating an order and log a configuration warning. This avoids paying for an order that is known to be insufficient.

## API Behavior

Order creation succeeds only on HTTP `202`.

Polling status handling:

- Success: `ACTIVE`
- Pending: `QUEUED`, `PENDING`, `PROCESSING`
- Failure: `FAILED`, `CANCELLED`, `COMPLETED`, `unknown`

`QUEUED` is accepted as pending for create-response compatibility. The ProfeeX docs show `QUEUED` in order creation responses even though the status endpoint table focuses on `PENDING` and `PROCESSING`.

If ProfeeX rejects the order, polling times out, returns a failure status, or the on-chain resource check still fails after `ACTIVE`, the sweep stops without broadcasting the TRC20 transfer.

## Sweep Integration

`tasks.py` should use explicit provider-mode control flow:

```python
use_external_energy_provider = config.ENERGY_PROVIDER in {"refee", "profeex"}
use_staking_energy_provider = (
    config.ENERGY_PROVIDER == "staking" and config.ENERGY_DELEGATION_MODE
)
use_energy_provider = use_external_energy_provider or use_staking_energy_provider
```

- If energy already meets the provider threshold, do not rent energy.
- If bandwidth is missing and `BANDWIDTH_PROVIDER=profeex`, rent bandwidth before energy provisioning.
- If ProfeeX energy acquisition fails, terminate the sweep.

The burn fallback remains scoped to the existing re:Fee behavior and should not be broadened. Even when `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT=True`, `ENERGY_PROVIDER="profeex"` must not call `_fund_onetime_for_trc20_burn()` after provider acquisition failure.

## Tests

Required tests:

- `Settings` accepts `ENERGY_PROVIDER="profeex"` only when `PROFEEX` is configured.
- `Settings` rejects invalid ProfeeX fixed energy and bandwidth order amounts outside provider limits.
- `get_energy_provider()` returns the ProfeeX provider for `ENERGY_PROVIDER="profeex"`.
- ProfeeX energy acquisition calls `/delegation/buyenergy` with `volume=fixed_energy_order_amount`, `days=energy_duration_label`, and `currency`.
- ProfeeX energy acquisition skips API calls when available energy is already at or above `fixed_energy_order_amount - 500`.
- ProfeeX energy acquisition verifies on-chain energy after `ACTIVE`.
- ProfeeX implements `release_energy()` as a no-op and the successful sweep path can call it safely.
- ProfeeX bandwidth acquisition uses `fixed_bandwidth_order_amount`.
- ProfeeX bandwidth acquisition fails before API order creation when `fixed_bandwidth_order_amount < required_bandwidth`.
- Task-level sweep tests prove `ENERGY_PROVIDER="profeex"` enters provider mode, rents configured bandwidth before energy when needed, stops on ProfeeX acquisition failure, and does not use the re:Fee burn fallback.
- Existing re:Fee tests keep passing.

## Deployment Docs

Update `docs/DEPLOYMENT.md` with the new ProfeeX energy example:

```yaml
ENERGY_PROVIDER: "profeex"
BANDWIDTH_PROVIDER: "profeex"
PROFEEX: '{"api_key":"REPLACE_WITH_PROFEEX_API_KEY","energy_duration_label":"1h","bandwidth_duration_label":"1h","currency":"TRX","fixed_energy_order_amount":65000,"fixed_bandwidth_order_amount":350}'
```

Document that ProfeeX balance is paid in `currency`, and that `fixed_energy_order_amount` / `fixed_bandwidth_order_amount` are order sizes, not API min/max limit fields.

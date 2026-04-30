# Phase 2 Pattern Map

**Date:** 2026-04-30

| Target | Role | Closest Analog | Pattern to Reuse |
|---|---|---|---|
| `app/refee.py` | New typed config model | `app/custom/aml/schemas.py`, `app/schemas.py` | Pydantic `BaseModel`, `Literal`, `SecretStr`, simple validators only when needed |
| `app/config.py` | Settings surface + cross-field validator | `EXTERNAL_DRAIN_CONFIG`, `MULTISERVER_CONFIG_JSON`, `validate_external_drain_config_states` | Top-level Settings fields, nested config object, fail-fast validator |
| `app/energy_provider.py` | Strategy implementation | `StakingEnergyProvider` | Constructor accepts `tron_client=None`; lazy config/client lookup in `acquire`; return `False` instead of raising |
| `app/tasks.py` | Sweep control flow | Phase 1 provider wiring in `transfer_trc20_from` | Bind provider once with selected `tron_client`; keep transfer build/sign/broadcast unchanged |

## Concrete Patterns

### Lazy provider client

Current staking pattern:

```python
class StakingEnergyProvider(EnergyProvider):
    def __init__(self, tron_client=None):
        self.tron_client = tron_client

    def acquire(...):
        tron_client = self.tron_client or ConnectionManager.client()
```

Refee must match this so multi-server mode does not switch fullnodes mid-sweep.

### Settings validator

Existing config validator style:

```python
@field_validator("EXTERNAL_DRAIN_CONFIG", mode="after")
@classmethod
def validate_external_drain_config_states(cls, value):
    ...
```

For a cross-field check use Pydantic v2 `model_validator(mode="after")`:

```python
@model_validator(mode="after")
def validate_refee_config_state(self):
    if self.ENERGY_SOURCE == "refee" and self.REFEE is None:
        raise ValueError("REFEE must be configured when ENERGY_SOURCE='refee'")
    return self
```

### Existing burn path to extract

The current burn branch in `transfer_trc20_from` checks main balance, sends
`INTERNAL_TX_FEE` from main to onetime, waits for broadcast, logs the fee tx, and
then falls through to the common token transfer. Extract this exact block into a
helper so re:Fee failure can use it without duplicating code.

### Logging

Use `from .logging import logger`. Do not use `print`. Avoid logging:

- `REFEE.api_key`
- full request headers
- `SecretStr` values

Safe to log:

- order id
- status transitions
- HTTP status codes
- response `error`
- `txn_hash`
- delegation latency

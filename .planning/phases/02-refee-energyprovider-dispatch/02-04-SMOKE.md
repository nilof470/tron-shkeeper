# Phase 2 Smoke Verification

## Structural checks

### compileall

Command:

```bash
PYTHONPYCACHEPREFIX=/tmp/tron-shkeeper-pycache /tmp/tron-shkeeper-py312-venv/bin/python -m compileall app/refee.py app/config.py app/energy_provider.py app/tasks.py
```

Result: PASS.

### Provider dispatch and selected client

Command:

```bash
PYTHONPYCACHEPREFIX=/tmp/tron-shkeeper-pycache /tmp/tron-shkeeper-py312-venv/bin/python -c 'import ast, pathlib; src=pathlib.Path("app/tasks.py").read_text(); tree=ast.parse(src); fn=next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name=="transfer_trc20_from"); calls=[n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "get_energy_provider"]; assert calls; assert any(kw.arg=="tron_client" for c in calls for kw in c.keywords); print("selected client provider call OK")'
```

Output:

```text
selected client provider call OK
```

Result: PASS. `transfer_trc20_from` still passes the sweep-selected
`tron_client` into `get_energy_provider(tron_client=tron_client)`.

### Staking-only bandwidth gate

Command:

```bash
grep -n "if use_staking_energy_provider" -A22 app/tasks.py
```

Evidence:

```text
if use_staking_energy_provider:
    _, energy_delegator_pub = get_energy_delegator()
    need_bw = (
        config.BANDWIDTH_PER_DELEGE_CALL
        + config.BANDWIDTH_PER_UNDELEGATE_CALL
        + config.BANDWIDTH_PER_TRX_TRANSFER
    )
    logger.info(f"Estimated bandwidth requirement: {need_bw}")
    logger.info("Check energy delegator bandwidth")
    if has_free_bw(energy_delegator_pub, need_bw):
```

Result: PASS. re:Fee mode does not run the staking energy-delegator bandwidth
check.

### Fallback bandwidth bypass

Command:

```bash
grep -n "if not used_trx_burn_fallback" -A8 app/tasks.py
```

Evidence:

```text
if not used_trx_burn_fallback:
    if not has_free_bw(
        onetime_publ_key, config.BANDWIDTH_PER_TRC20_TRANSFER_CALL
    ):
```

Result: PASS. TRX-burn fallback can proceed without the provider-branch
free-bandwidth gate blocking it.

## Config validation

### Default staking config

Command:

```bash
DATABASE=/tmp/tron-shkeeper-smoke-data/database.db DB_URI=sqlite:////tmp/tron-shkeeper-smoke-data/tron.db PYTHONPYCACHEPREFIX=/tmp/tron-shkeeper-pycache /tmp/tron-shkeeper-py312-venv/bin/python -c 'from app.config import Settings; s=Settings(); assert s.ENERGY_SOURCE == "staking"; assert s.REFEE is None; print("default staking OK")'
```

Output:

```text
default staking OK
```

Result: PASS.

### re:Fee without REFEE

Command:

```bash
ENERGY_SOURCE=refee DATABASE=/tmp/tron-shkeeper-smoke-data/database.db DB_URI=sqlite:////tmp/tron-shkeeper-smoke-data/tron.db PYTHONPYCACHEPREFIX=/tmp/tron-shkeeper-pycache /tmp/tron-shkeeper-py312-venv/bin/python -c 'from app.config import Settings; Settings()'
```

Expected failure output contains:

```text
REFEE must be configured when ENERGY_SOURCE='refee'
```

Result: PASS. Pydantic raises at startup.

### re:Fee with REFEE JSON

Command:

```bash
env ENERGY_SOURCE=refee REFEE='{"api_key":"secret"}' DATABASE=/tmp/tron-shkeeper-smoke-data/database.db DB_URI=sqlite:////tmp/tron-shkeeper-smoke-data/tron.db PYTHONPYCACHEPREFIX=/tmp/tron-shkeeper-pycache /tmp/tron-shkeeper-py312-venv/bin/python -c 'from app.config import Settings; s=Settings(); assert s.ENERGY_SOURCE == "refee"; assert s.REFEE.rent_duration_label == "1h"; print("refee config OK", type(s.REFEE.api_key).__name__)'
```

Output:

```text
refee config OK SecretStr
```

Result: PASS.

## Mocked re:Fee provider happy path

Temporary script: `/tmp/refee_provider_smoke.py` (not committed).

Command:

```bash
env PYTHONPATH=. ENERGY_SOURCE=refee REFEE='{"api_key":"secret"}' DATABASE=/tmp/tron-shkeeper-smoke-data/database.db DB_URI=sqlite:////tmp/tron-shkeeper-smoke-data/tron.db PYTHONPYCACHEPREFIX=/tmp/tron-shkeeper-pycache /tmp/tron-shkeeper-py312-venv/bin/python /tmp/refee_provider_smoke.py
```

Assertions covered:

- `requests.post` received `timeout=10`.
- POST body was:
  `{"address": "TREFEEFAKEADDRESS", "amount": 68250, "resource": "energy", "duration_label": "1h"}`.
- `requests.get` polled `/api/rent_resource/orders/order-1` with `timeout=10`.
- status sequence `pending -> delegated` returned `True`.
- selected `tron_client.get_account_resource(receiver)` was called.
- `EnergyLimit >= energy_required` was required before success.

Output:

```text
provider happy path OK
```

Result: PASS.

## Mocked failure + fallback path

Temporary script: `/tmp/refee_fallback_smoke.py` (not committed).

Command:

```bash
env PYTHONPATH=. DATABASE=/tmp/tron-shkeeper-fallback-smoke/database.db DB_URI=sqlite:////tmp/tron-shkeeper-fallback-smoke/tron.db PYTHONPYCACHEPREFIX=/tmp/tron-shkeeper-pycache /tmp/tron-shkeeper-py312-venv/bin/python /tmp/refee_fallback_smoke.py
```

Assertions covered:

- `provider.acquire(...)` returned `False`.
- `ENERGY_SOURCE=refee` and `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT=true`
  reached `_fund_onetime_for_trc20_burn`.
- TRX funding transaction broadcast once.
- token transfer still broadcast.
- `used_trx_burn_fallback=True` caused the provider-branch free-bandwidth check
  to be skipped (`has_free_bw_calls == []`).

Output:

```text
fallback path OK
```

Result: PASS.

## Import sanity

Command:

```bash
DATABASE=/tmp/tron-shkeeper-smoke-data/database.db DB_URI=sqlite:////tmp/tron-shkeeper-smoke-data/tron.db PYTHONPYCACHEPREFIX=/tmp/tron-shkeeper-pycache /tmp/tron-shkeeper-py312-venv/bin/python -c 'from app.energy_provider import EnergyProvider, StakingEnergyProvider, RefeeEnergyProvider, get_energy_provider; from app.tasks import transfer_trc20_from, undelegate_energy, transfer_trx_from; print("import sanity OK")'
```

Output:

```text
import sanity OK
```

Result: PASS.

## Live spike 003 status

LIVE SPIKE PENDING - no operator `REFEE_API_KEY`, topped-up re:Fee balance, and
`REFEE_TEST_TRON_ADDRESS` were available in this session.

Companion runbook/probe exists at:

```text
/Users/test/PycharmProjects/shkeeper.io/.planning/spikes/003-refee-rent-order-lifecycle/refee_rent_lifecycle.py
```

Until that probe is run live, keep `REFEE.timeout_sec=60` and
`REFEE.poll_interval_sec=2.0` provisional.

## Result

Phase 2 structural, config, provider, fallback, and import smoke checks passed.
Live re:Fee latency/rate-limit/refund behavior remains pending companion spike 003.

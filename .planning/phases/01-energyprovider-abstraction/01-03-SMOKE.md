# Phase 1 Smoke Verification

## Structural verification

### 1. RPC sequence preservation

The original `delegate_energy` closure at `app/tasks.py:122-184` (pre-refactor, `HEAD~2`) performs five chain interactions. The lifted `StakingEnergyProvider.acquire` performs the same five in the same order.

| # | Operation | Original location | Lifted location |
|---|-----------|-------------------|-----------------|
| 1 | `wallet/getcandelegatedmaxsize` RPC | tasks.py:124-127 (`HEAD~2`) | energy_provider.py:79-82 |
| 2 | `tron_client.trx.delegate_resource(...).build()` | tasks.py:149-154 (`HEAD~2`) | energy_provider.py:104-109 |
| 3 | `unsigned_tx.sign(energy_delegator_priv)` | tasks.py:155 (`HEAD~2`) | energy_provider.py:110 |
| 4 | `signed_tx.broadcast().wait()` | tasks.py:158 (`HEAD~2`) | energy_provider.py:113 |
| 5 | `tron_client.get_account_resource(receiver)` recheck | tasks.py:168-170 (`HEAD~2`) | energy_provider.py:123 |

Operation order check:

```text
['wallet/getcandelegatedmaxsize', 'delegate_resource', 'sign', 'broadcast().wait', 'get_account_resource']
ordered: True
```

Result: pass.

### 2. Math preservation

The closure `calc_sun_for_energy_delegation` at pre-refactor `app/tasks.py:115-120` computed:

```text
trx = ceil(TotalEnergyWeight * energy / TotalEnergyLimit)
trx *= ENERGY_DELEGATION_MODE_ENERGY_DELEGATION_FACTOR
return int(trx * 1_000_000)
```

The lifted `StakingEnergyProvider._calc_sun_for_energy_delegation` is at `app/energy_provider.py:148-153`.

Diff of extracted function bodies:

```text
Original body:
trx: int = math.ceil(
            (res["TotalEnergyWeight"] * energy) / res["TotalEnergyLimit"]
        )
trx *= config.ENERGY_DELEGATION_MODE_ENERGY_DELEGATION_FACTOR
return int(trx * 1_000_000)
---
Lifted body:
trx: int = math.ceil(
            (res["TotalEnergyWeight"] * energy) / res["TotalEnergyLimit"]
        )
trx *= config.ENERGY_DELEGATION_MODE_ENERGY_DELEGATION_FACTOR
return int(trx * 1_000_000)
---
Result: byte-identical
```

Result: byte-identical.

### 3. Release dispatch preservation

Original at `app/tasks.py:412-416` (`HEAD~2`):

```python
if config.ENERGY_DELEGATION_MODE:
    if config.DEVMODE_CELERY_NODELAY:
        undelegate_energy(onetime_publ_key)
    else:
        undelegate_energy.delay(onetime_publ_key)
```

After Plan 02 wiring, the post-transfer block is at `app/tasks.py:346-347`:

```python
if config.ENERGY_DELEGATION_MODE:
    provider.release(onetime_publ_key)
```

Inside `StakingEnergyProvider.release`:

```python
def release(self, receiver: str) -> None:
        from app.tasks import undelegate_energy

        if config.DEVMODE_CELERY_NODELAY:
            undelegate_energy(receiver)
        else:
            undelegate_energy.delay(receiver)
```

The combined behavior has the same `if config.ENERGY_DELEGATION_MODE:` guard on the caller side, the same `DEVMODE_CELERY_NODELAY` check on the provider side, and the same dispatch to the `undelegate_energy` Celery task.

Result: equivalent.

### 4. Closures removed from transfer_trc20_from

Command:

```bash
python -c "import ast; src = open('app/tasks.py').read(); tree = ast.parse(src); fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'transfer_trc20_from'); inner = [n.name for n in ast.walk(fn) if isinstance(n, ast.FunctionDef) and n is not fn]; print('Inner functions:', inner)"
```

Actual output:

```text
Inner functions: []
```

Result: pass.

### 5. undelegate_energy Celery task untouched

AST source comparison of `app/tasks.py:undelegate_energy` between `HEAD~2` and current branch:

```text
unchanged
```

Result: unchanged.

### 6. config.py and requirements.txt unchanged

Command:

```bash
git diff HEAD~2 -- app/config.py requirements.txt | wc -l
```

Actual:

```text
0
```

Result: pass.

### 7. Module-level import sanity

Command:

```bash
DATABASE=/tmp/tron-shkeeper-smoke-data/database.db \
DB_URI=sqlite:////tmp/tron-shkeeper-smoke-data/tron.db \
PYTHONPYCACHEPREFIX=/tmp/tron-shkeeper-pycache \
/tmp/tron-shkeeper-py312-venv/bin/python -c 'from app.energy_provider import EnergyProvider, StakingEnergyProvider, get_energy_provider; from app.tasks import transfer_trc20_from, undelegate_energy, transfer_trx_from; print("OK")'
```

Actual:

```text
OK
```

Result: pass.

Note: the repository `.venv` is Python 3.9.6 and cannot import the existing `app/config.py` because the codebase already uses Python 3.10+ union syntax. Import verification used a temporary Python 3.12 venv at `/tmp/tron-shkeeper-py312-venv`. The existing `app.tasks` import also requires a SQLite `keys` table because `Wallet.main_account` queries at import time, so the smoke used a temporary schema at `/tmp/tron-shkeeper-smoke-data/database.db`.

## Live / fixture-replay invocation

**Mode used:** B  
**Date:** 2026-04-30T12:50:00Z  
**Environment:** local-stub with mocked tronpy client, valid generated TRON base58 addresses, temporary SQLite schema under `/tmp`

### Captured evidence

Command result:

```text
MODE_B_OK
onetime= THHsfg2eNiv6MSXC4y5d4t5wkvRVADRKiF
main= TCNkawTmcQgYSU8nP8cHswT1QPjharxJr7
delegator= TEdea7WvtoCNceWPwaz7JbkBjbb6omTQcL
delegate_resource_call= call(owner='TEdea7WvtoCNceWPwaz7JbkBjbb6omTQcL', receiver='THHsfg2eNiv6MSXC4y5d4t5wkvRVADRKiF', balance=650000000, resource='ENERGY')
undelegate_delay_call= call('THHsfg2eNiv6MSXC4y5d4t5wkvRVADRKiF')
result= {'tx_trx_res': None, 'tx_token': {'result': True, 'txid': 'TOKEN_TXID'}}
```

Representative captured log excerpt:

```text
Check ONETIME=THHsfg2eNiv6MSXC4y5d4t5wkvRVADRKiF USDT balance
Balance OK: 10 USDT. Threshold: 5 USDT
Initiating TRC20 tokens transfer from ONETIME=THHsfg2eNiv6MSXC4y5d4t5wkvRVADRKiF to MAIN=TCNkawTmcQgYSU8nP8cHswT1QPjharxJr7 in ENERGY DELEGATION MODE
Estimated bandwidth requirement: 828
Check energy delegator bandwidth
Using free bandwidth
Onetime THHsfg2eNiv6MSXC4y5d4t5wkvRVADRKiF is already on chain, skipping activation. Resource details onetime_address_resources={'EnergyLimit': 0, 'TotalEnergyWeight': 1000, 'TotalEnergyLimit': 100000}
Estimate the amount of energy needed to make transfer
Estimated amount of energy for transfer is: 65000
Check the energy of onetime address
Onetime account THHsfg2eNiv6MSXC4y5d4t5wkvRVADRKiF has 0 of 65000 energy
Check if energy was alread delegated
No delagated energy found
Requesting energy provider to provision 65000 energy on THHsfg2eNiv6MSXC4y5d4t5wkvRVADRKiF
Check if energy delegator account can delegate energy
delegetable_sun=1000000000 sun_to_delegate=650000000
Energy delegator has enough energy
Delegating energy to onetime account
TX json size: 25
Delegated 65000 energy to onetime account THHsfg2eNiv6MSXC4y5d4t5wkvRVADRKiF with TXID: DELEGATE_TXID
{'result': True, 'txid': 'DELEGATE_TXID'}
Recheck resources of the onetime address after energy delegation
receiver='THHsfg2eNiv6MSXC4y5d4t5wkvRVADRKiF' onetime_energy_available=65000 energy_needed=65000
Energy successfuly delegated
10.0 USDT sent to TCNkawTmcQgYSU8nP8cHswT1QPjharxJr7 with TOKEN_TXID. Details: {'result': True, 'txid': 'TOKEN_TXID'}
```

### Provider call sites observed

- `provider.acquire(...)` invocation: asserted-on-mock through `delegate_resource_call`.
- `provider.release(...)` invocation: asserted-on-mock through `undelegate_delay_call`.
- `wallet/getcandelegatedmaxsize` RPC: mocked, returned `{"max_size": 1000000000}`.
- `tron_client.trx.delegate_resource`: mocked and asserted with `owner`, `receiver`, `balance=650000000`, `resource="ENERGY"`.
- `signed_tx.broadcast().wait()`: mocked and asserted via delegate tx `wait_called`.
- TRC-20 transfer broadcast: mocked and asserted via token tx `wait_called`; returned `TOKEN_TXID`.
- `undelegate_energy.delay`: asserted-on-mock with the onetime address.

### Anomalies / deviations

None observed in the refactored code path. The first local-stub attempt used syntactically invalid fake TRON addresses and failed inside `trx_abi.encode_single` before reaching provider code; the stub was corrected to generate valid base58 TRON addresses from `tronpy.keys.PrivateKey`.

## Post-review fix verification

**Date:** 2026-04-30T13:13:42Z  
**Reason:** `01-REVIEW.md` identified that `StakingEnergyProvider.acquire()` selected a second `ConnectionManager.client()` instead of reusing the sweep's already-selected `tron_client`.

### RED check before fix

The regression stub allowed only one `ConnectionManager.client()` call during `transfer_trc20_from()`. Before the fix, it failed at provider acquire:

```text
AssertionError: second ConnectionManager.client() call
```

### GREEN check after fix

After `f68e27f`, `transfer_trc20_from()` passes `tron_client` into `get_energy_provider(tron_client=tron_client)`, and `StakingEnergyProvider.acquire()` reuses it.

```text
OK: Mode B smoke reused selected client and completed acquire/transfer/release
connection_manager_client_calls= 1
delegate_resource_call= call(owner='TEdea7WvtoCNceWPwaz7JbkBjbb6omTQcL', receiver='THHsfg2eNiv6MSXC4y5d4t5wkvRVADRKiF', balance=650000000, resource='ENERGY')
result= {'tx_trx_res': None, 'tx_token': {'result': True, 'txid': 'TOKEN_TX'}}
```

### Cleanup

`4902690` removed stale `json` and `math` imports from `app/tasks.py`; AST checks confirmed neither name is referenced in the file.

## Human verification

**Reviewed by:** operator  
**Date:** 2026-04-30T13:24:28Z  
**Verdict:** APPROVED - Phase 1 refactor is behavior-identical to current master for the staking happy path. Ready for Phase 2.

**Notes:** Approval was given in chat ("все подтверждаю"). Review findings were fixed and documented in `01-REVIEW-FIX.md`; post-review smoke confirms the staking provider reuses the sweep-selected TRON client.

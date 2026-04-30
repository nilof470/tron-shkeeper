---
phase: 01-energyprovider-abstraction
reviewed: 2026-04-30T13:04:10Z
depth: standard
files_reviewed: 2
files_reviewed_list:
  - app/energy_provider.py
  - app/tasks.py
findings:
  critical: 0
  warning: 1
  info: 1
  total: 2
status: issues_found
---

# Phase 1: Code Review Report

**Reviewed:** 2026-04-30T13:04:10Z  
**Depth:** standard  
**Files Reviewed:** 2  
**Status:** issues_found

## Summary

Reviewed the Phase 1 source changes: the new `app/energy_provider.py` provider abstraction and the `transfer_trc20_from` refactor in `app/tasks.py`.

The mechanical extraction mostly preserves the old staking flow, and the existing structural checks pass. One behavior risk remains: the lifted staking provider now opens its own TRON client instead of using the client already selected by `transfer_trc20_from`. There is also minor import cleanup left in `app/tasks.py`.

## Warnings

### WR-01: Staking provider no longer uses the transfer's selected TRON client

**File:** `app/energy_provider.py:67` and `app/tasks.py:95`

**Issue:** Before the refactor, the nested `delegate_energy` closure used the `tron_client` created once in `transfer_trc20_from`. After the refactor, `transfer_trc20_from` still creates a client at `app/tasks.py:95`, but `StakingEnergyProvider.acquire()` creates a second client via `ConnectionManager.client()` at `app/energy_provider.py:67`.

That is not behavior-identical in deployments using `MULTISERVER_CONFIG_JSON`: `ConnectionManager.client()` reads the current server id from the database each time. If the best-server refresh or an operator action changes `current_server_id` while a sweep is in progress, pre-delegation checks/token transfer can use one fullnode while delegation/recheck use another. This can introduce inconsistent state reads or transient rejection around the high-stakes delegation/transfer path.

**Fix:** Preserve the selected client across the whole sweep. One low-impact option is to inject the client when constructing the staking provider:

```python
class StakingEnergyProvider(EnergyProvider):
    def __init__(self, tron_client=None):
        self.tron_client = tron_client

    def acquire(...):
        tron_client = self.tron_client or ConnectionManager.client()
        ...


def get_energy_provider(tron_client=None) -> EnergyProvider:
    return StakingEnergyProvider(tron_client=tron_client)
```

Then bind it in `transfer_trc20_from` as:

```python
provider = get_energy_provider(tron_client=tron_client)
```

This keeps Phase 2 extension points intact while matching the original single-client behavior.

## Info

### IN-01: `json` and `math` imports are now unused in `app/tasks.py`

**File:** `app/tasks.py:7-8`

**Issue:** Removing the nested delegation closures moved the only `json` and `math` module usages out of `app/tasks.py`. An AST name scan confirms neither module name is referenced in the current file.

**Fix:** Remove the unused imports from `app/tasks.py`:

```python
-import json
-import math
```

## Verification Performed

- `PYTHONPYCACHEPREFIX=/tmp/tron-shkeeper-pycache /tmp/tron-shkeeper-py312-venv/bin/python -m compileall app/tasks.py app/energy_provider.py`
- `DATABASE=/tmp/tron-shkeeper-smoke-data/database.db DB_URI=sqlite:////tmp/tron-shkeeper-smoke-data/tron.db PYTHONPYCACHEPREFIX=/tmp/tron-shkeeper-pycache /tmp/tron-shkeeper-py312-venv/bin/python -c 'from app.energy_provider import EnergyProvider, StakingEnergyProvider, get_energy_provider; from app.tasks import transfer_trc20_from, undelegate_energy, transfer_trx_from; print("OK")'`
- AST check confirmed `transfer_trc20_from` has no nested functions.
- `git diff master...HEAD -- app/config.py requirements.txt | wc -l` returned `0`.

---

_Reviewed: 2026-04-30T13:04:10Z_  
_Reviewer: Codex inline gsd-code-review fallback_  
_Depth: standard_

---
status: issues_found
phase: 03-live-e2e-validation-docs
depth: subagent-review
files_reviewed: 4
files:
  - app/tasks.py
  - app/energy_provider.py
  - app/utils.py
  - tests/test_refee_energy_accounting.py
findings:
  critical: 0
  high: 1
  medium: 2
  low: 0
  total: 3
reviewed_at: 2026-04-30
reviewer_agent: 019ddf23-a718-7c52-b8fe-6a46e8f6f391
---

# Phase 3 Final Subagent Review

## Open Findings

### HIGH - used energy was counted as available energy

**Location:** `app/tasks.py`, `app/energy_provider.py`

The sweep path compared estimated transfer energy against `EnergyLimit`.
TRON account resources also expose `EnergyUsed`, so the actually available
energy is `max(EnergyLimit - EnergyUsed, 0)`. If a one-time account had
`EnergyLimit=100000` and `EnergyUsed=90000`, the previous logic could skip
energy acquisition even though only `10000` energy was currently usable.

**Recommendation:** centralize available-energy accounting and use it everywhere
the sweep decides whether energy is already sufficient.

### MEDIUM - re:Fee mode could confuse delegated bandwidth with delegated energy

**Location:** `app/tasks.py`

The legacy staking path checks
`get_delegated_resource_account_index_v2(...).fromAccounts` before deciding
whether more energy may be delegated. In re:Fee mode, this gate is too broad:
`fromAccounts` can also exist because the operator manually delegated bandwidth
to the one-time address. Treating that as existing energy can stop a valid
re:Fee rental unless additional energy delegation is enabled.

**Recommendation:** in re:Fee mode, decide from actual available energy and rent
the missing delta. Keep the legacy delegated-resource gate only for staking mode.

### MEDIUM - re:Fee top-up orders used total requirement instead of missing delta

**Location:** `app/energy_provider.py`

`RefeeEnergyProvider.acquire(...)` received both the missing energy delta and
the full minimum energy required for post-rental verification, but the paid
order amount was sized from the full requirement. That over-rents when an account
already has some usable energy.

**Recommendation:** create the re:Fee order from `energy_to_provision` plus the
configured overprovision factor, then verify the final available energy against
`minimum_energy_required`.

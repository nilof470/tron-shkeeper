# Project: tron-shkeeper — re:Fee energy rental integration

**Created:** 2026-04-30
**Vendor target:** [re:Fee](https://api.refee.bot/v2) (refeebot)
**Parent feature in shkeeper.io main:** Static Address Mode (`README.md#static-address-mode-advanced`)

## Vision

Add a second energy source to `tron-shkeeper`'s sweep flow: instead of (or alongside) the current TRX-staking-based delegation (`ENERGY_DELEGATION_MODE`), allow the sidecar to rent TRON energy from re:Fee per individual sweep. The integration is transparent to operators (one new env var `ENERGY_SOURCE=refee`), to users (the sweep result is the same — USDT-TRC20 transfer with zero TRX burned), and to the rest of the SHKeeper system (no changes to `shkeeper.io` main repo).

## Why

Energy delegation via TRX staking ties up substantial capital (≈40 TRX needs to be frozen for ~12,000 daily transferable energy units; for typical SHKeeper deployments running hundreds of user-wallets that's hundreds-to-thousands of TRX locked). Renting energy via re:Fee at 37 sun/unit for 1h tier costs ~2.41 TRX per USDT-TRC20 transfer with **no capital lockup**. For typical merchant-payment activity (1–4 transfers per user-wallet per month), per-sweep rental is **~50× cheaper** than always-on subscription approaches and **~5× cheaper** than the burn-TRX baseline (13.65 TRX/transfer at 65k energy and 210 sun/energy).

Source: `.planning/spikes/001-refee-auth-and-economics/break_even.md` in companion repo `shkeeper.io`.

## In scope

- Replace inner `delegate_energy()` call inside `transfer_trc20_from` (`app/tasks.py:354`) with a pluggable `EnergyProvider` abstraction.
- New `RefeeEnergyProvider` calling `POST /api/rent_resource/orders` + polling `GET /api/rent_resource/orders/{id}` until `status == "delegated"`.
- Existing freeze-v2 logic preserved as `StakingEnergyProvider`, dispatched via new env var `ENERGY_SOURCE in {"staking", "refee"}` (default `"staking"` for backward compatibility).
- Fallback to TRX-burn flow on re:Fee failure, gated by existing `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT`.
- New env vars in `app/config.py` for re:Fee API key, base URL, polling interval, timeout, over-provision factor.
- Idempotency on celery retry handled implicitly via existing on-chain `EnergyLimit` check at `app/tasks.py:297-303` — no schema or external_id needed.

## Out of scope (v1)

- AML alt path (`app/custom/aml/classes.py`) — currently doesn't use energy delegation; will be considered separately if desired.
- re:Fee `auto_charging` and `always_charged` modes — economics rejected by user; only `rent_resource` 1h is used.
- re:Fee `/api/functions/aml` and `/api/functions/activate` — out of scope explicitly per user. AML stays on `aml-shkeeper` (provider: `amlbot`).
- Bandwidth purchase from re:Fee — TRC-20 transfer requires only 346 bytes; free 1500/day allocation covers all realistic per-address usage.
- Webhook-driven status updates — re:Fee API offers no webhooks (verified against live `openapi.json`); polling is required.
- Sandbox/testnet validation — re:Fee API has no testnet; live validation is on mainnet with small (~3 TRX) balance.

## Out of scope (v2 / later)

- Per-token policy (e.g. always rent for USDT but burn for USDC).
- Threshold-based hybrid (auto-switch to `always_charged` when a wallet's transfer rate exceeds 3.3/day) — measure first, optimize later.
- UI exposure in `shkeeper.io` main repo for re:Fee balance / order history.

## Constraints

- **Self-host UX must not change.** Operators continue to install via `vsys-host/helm-charts`; the only changes from their side are two env vars (`ENERGY_SOURCE`, `REFEE_API_KEY`) and an updated `tron-shkeeper` image.
- **Backward compatibility.** Default `ENERGY_SOURCE=staking` keeps existing freeze-v2 behavior bit-identical for installations that don't opt in.
- **Personal fork.** Changes ship to `nilof470/tron-shkeeper`; not intended to be PR'd to `vsys-host/tron-shkeeper`.

## Success criteria

1. Configured with `ENERGY_SOURCE=refee` and a valid `REFEE_API_KEY`, the sidecar performs a USDT-TRC20 sweep on a user-wallet that has zero TRX balance, with **zero TRX burned** for the transfer.
2. With `ENERGY_SOURCE=staking` (default) and existing freeze-v2 setup, behavior is **bit-identical** to upstream master (no regressions in existing deployments).
3. With `ENERGY_SOURCE=refee` but re:Fee unreachable, the sidecar falls back according to `ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT` (either burns or skips this cycle).
4. After a celery retry mid-flow (e.g. sidecar restart after re:Fee accepts but before broadcast), the next attempt observes the active delegation and proceeds without double-paying.

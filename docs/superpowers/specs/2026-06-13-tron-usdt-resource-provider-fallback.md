# TRON USDT Resource Provider Fallback Spec

## Goal

Use ProfeeX as the primary resource provider and re:Fee as fallback for both TRON USDT payout and TRON USDT sweep. Both flows must follow the same pre-broadcast sequence:

1. Estimate required energy.
2. Check sender/source bandwidth and rent bandwidth only if the sender is short.
3. Check sender/source energy and rent energy only if the sender is short.
4. Re-check on-chain resources.
5. Broadcast the TRC20 USDT transfer only after resources are ready.

## Scope

Covered flows:

- Payout: `fee_deposit -> customer destination`.
- Sweep: `onetime/client address -> fee_deposit`.

Covered providers:

- Primary: ProfeeX.
- Fallback: re:Fee.

Non-goals:

- Do not add NETTS as a provider.
- Do not use a hardcoded `131000` energy fallback.
- Do not broadcast a TRC20 transaction after a failed estimate or failed resource rental.

## Source and Destination Semantics

Every resource operation has a sender/source address and a receiver/destination address.

For payout:

- `source_address = fee_deposit`
- `destination_address = payout destination`

For sweep:

- `source_address = onetime/client address`
- `destination_address = fee_deposit`

Bandwidth is consumed by the sender/source transaction owner. The system must check and rent bandwidth on `source_address`.

Energy is consumed by the sender/source transaction owner for the TRC20 contract call. The system must check and rent energy on `source_address`.

## Estimate Semantics

Estimate provider chain:

1. ProfeeX estimate.
2. re:Fee estimate.

ProfeeX estimate currently uses:

- `GET /delegation/fee`
- input: `receiver_address`
- output includes `energy_required`, `is_new_address`, `trx_burned`

re:Fee estimate must use the local OpenAPI contract in `docs/openapi-refeebot.json`:

- `GET /api/functions/cost/{address}`
- header: `X-API-Key`
- input: sender/source `address`
- output: `{ "cost": <energy_required> }`

The re:Fee estimate endpoint does not return ProfeeX-style destination activation fields. If payout destination activation status is needed, it must be checked through TRON account state or the existing activation flow. re:Fee estimate must not be treated as proof that the destination is activated.

If both estimate providers fail, the transfer must stop before resource rental and before broadcast. For payout execution, this is a transient pre-broadcast failure. For sweep, the task returns without broadcasting and can be retried by the existing sweep scheduling.

## Destination Activation Semantics

Payout may require activation of the customer destination address before USDT transfer. Destination activation follows its own provider chain:

1. ProfeeX activation.
2. re:Fee activation.

ProfeeX activation currently uses:

- `POST /activation/activate`
- input: destination address
- output includes `task_id`, then the order is polled until active/completed.

re:Fee activation must use the local OpenAPI contract in `docs/openapi-refeebot.json`:

- `POST /api/functions/activate`
- query: `address=<destination_address>`
- header: `X-API-Key`
- successful response: `200` with `TransactionSchema`, including `txn_hash`
- `400` means the wallet does not require activation and should be treated as already active

If ProfeeX activation is unavailable before an activation task is accepted because of network errors, timeout, HTTP 5xx, rate limiting, or insufficient ProfeeX balance, the system must try re:Fee activation. If ProfeeX returns an accepted or ambiguous activation task, the system must not switch to re:Fee activation until that task is reconciled. If both activation providers are unavailable before acceptance, payout must return a transient pre-broadcast failure and must not rent transfer resources or broadcast the USDT transfer.

Sweep source activation is not changed by this fallback. Sweep source activation remains the existing flow that activates the onetime/client source address from `fee_deposit` when needed before resource provisioning.

## Provider Fallback Semantics

Primary provider is selected by existing config:

- `ENERGY_PROVIDER=profeex`
- `BANDWIDTH_PROVIDER=profeex`

Fallback provider should be generic for both payout and sweep:

- `TRON_USDT_RESOURCE_FALLBACK_PROVIDER=refee`

The old payout-only name introduced in the incomplete patch, `PAYOUT_RESOURCE_FALLBACK_PROVIDER`, must not be the final public config for this behavior.

Energy provider chain:

1. Primary `ENERGY_PROVIDER`, when it is an external provider.
2. `TRON_USDT_RESOURCE_FALLBACK_PROVIDER`, when configured and different from primary.

Bandwidth provider chain:

1. Primary `BANDWIDTH_PROVIDER`, when not disabled.
2. `TRON_USDT_RESOURCE_FALLBACK_PROVIDER`, when configured and different from primary.

If ProfeeX estimate is unavailable, the system tries re:Fee estimate. If ProfeeX bandwidth or energy rental fails before any accepted or ambiguous order signal, the system tries the matching re:Fee rental. All provider retries are still pre-broadcast.

Fallback is intentionally broad for ProfeeX failures when there is no confirmed or ambiguous resource rental yet:

- network errors;
- request timeouts;
- DNS/connect/read failures;
- HTTP 5xx;
- HTTP 408/429;
- rate limiting or temporary provider errors;
- insufficient ProfeeX provider balance;
- malformed or unexpected ProfeeX response before an order is accepted.

Fallback is not allowed for local configuration errors, invalid addresses, validation errors, auth/IP whitelist errors, or malformed provider responses after an accepted order signal.

If ProfeeX returns an order `task_id`, or an accepted-looking response without a usable id, treat it as potentially rented. The system must poll an existing ProfeeX order through the configured timeout/recheck policy, because a temporary poll failure can recover and the order may still become active. Fallback to re:Fee is not allowed after any accepted or ambiguous order signal, including terminal failed status, polling timeout, malformed accepted response, or post-active resource recheck failure. Those cases stop before broadcast and surface accepted-order metadata for retry/manual reconciliation.

Fallback eligibility matrix:

| Place | Failure | Fallback to re:Fee? | Result |
| --- | --- | --- | --- |
| ProfeeX estimate | network error, timeout, DNS/connect/read failure, HTTP 408/429/5xx | yes | Try re:Fee estimate. |
| ProfeeX estimate | invalid destination, local config missing, auth/IP error, validation error | no | Stop before resource rental and broadcast. |
| ProfeeX estimate | malformed response before any accepted order | yes | Try re:Fee estimate because no rental can exist. |
| ProfeeX activation/rent create | request failed, timeout, HTTP 408/429/5xx, insufficient ProfeeX balance | yes | Try re:Fee activation/rent. |
| ProfeeX activation/rent create | invalid address, local config missing, auth/IP error, validation error | no | Stop before broadcast. |
| ProfeeX activation/rent create | `202` with valid `task_id` | no | Poll ProfeeX first; do not switch providers if polling later fails. |
| ProfeeX activation/rent create | accepted-looking response without usable `task_id` | no | Treat as ambiguous accepted order; stop before broadcast and retry/alert rather than double-rent. |
| ProfeeX order polling | one or more transient poll failures before timeout | no | Keep polling the ProfeeX `task_id`. |
| ProfeeX order polling | terminal failed/canceled status | no | Stop before broadcast with accepted-order metadata; do not double-rent. |
| ProfeeX order polling | polling timeout | no | Stop before broadcast with accepted-order metadata; do not double-rent. |
| ProfeeX post-active on-chain recheck | resources still insufficient after all configured attempts | no | Stop before broadcast with accepted-order metadata; do not double-rent. |
| re:Fee estimate/activation/rent | network error, timeout, HTTP 408/429/5xx, insufficient re:Fee balance, order failed/insufficient_funds/canceled/timeout | no further provider | Payout gets transient pre-broadcast failure; sweep returns without broadcast. |
| re:Fee rent | accepted response without usable order id, malformed accepted response, order timeout, post-delegation read/recheck failure | no further provider | Stop before broadcast with accepted-order metadata; do not retry as a clean no-side-effect failure. |
| re:Fee activation | HTTP 400 "wallet does not require activation" | success | Treat destination as already active. |
| re:Fee rent | HTTP 400 "wallet does not exist or is not activated" | no further provider | Stop before broadcast; this is a source activation/state problem. |
| re:Fee any endpoint | auth/IP error, validation error, tariff not found | no further provider | Stop before broadcast and surface configuration/provider error. |

## Energy Amount Rules

Energy amount comes from the estimate chain. Do not use a fixed `131000` fallback.

For normal initialized USDT transfer cases, estimate should usually be around `65000`.

If an estimate provider returns a higher value, the provider must be asked to satisfy that higher value. re:Fee fixed-order logic must not cap a strict 131000 estimate down to `REFEE_FIXED_ENERGY_ORDER_AMOUNT=65000`.

`REFEE_FIXED_ENERGY_ORDER_AMOUNT` may remain as an order default or lower bound, but strict resource provisioning must order enough energy to satisfy `minimum_energy_required`.

## Bandwidth Amount Rules

Bandwidth requirement for one TRC20 transfer stays:

- `BANDWIDTH_PER_TRC20_TRANSFER_CALL`, currently `346`.

If current bandwidth on `source_address` is enough, no bandwidth order is created.

If current bandwidth is short and fallback reaches re:Fee, call re:Fee bandwidth rental with the required transfer bandwidth. re:Fee provider applies its own minimum order amount:

- `min_bandwidth_order_amount`, currently `1000`.

This means the code can request `346`, and re:Fee will submit `1000`.

## Payout Flow

1. Resolve `source_address = fee_deposit`.
2. Validate payout `destination`.
3. Read `source_address` account resources.
4. Estimate USDT transfer energy:
   - Try ProfeeX estimate with destination.
   - If unavailable and fallback is re:Fee, try re:Fee estimate with source.
   - If unavailable, return transient `RESOURCE_ESTIMATE_UNAVAILABLE`.
5. If destination is not activated:
   - Try ProfeeX activation first.
   - If ProfeeX activation is operationally unavailable, try re:Fee activation.
   - If both activation providers are unavailable, return transient activation error.
   - Do not treat a successful re:Fee energy estimate as destination activation proof, because re:Fee `/api/functions/cost/{address}` estimates source energy and does not report destination activation.
6. Check source bandwidth.
7. Rent bandwidth through provider chain only if source bandwidth is short.
8. Check source energy.
9. Rent energy through provider chain only if source energy is short.
10. Re-check source resources.
11. Broadcast payout only when energy and bandwidth deficits are zero.

## Sweep Flow

1. Resolve `source_address = onetime/client address`.
2. Resolve `destination_address = fee_deposit`.
3. Check guarded sweep eligibility before chain side effects.
4. Ensure source address is active through existing activation behavior.
5. Read `source_address` account resources.
6. Estimate USDT transfer energy:
   - Try ProfeeX estimate with destination.
   - If unavailable and fallback is re:Fee, try re:Fee estimate with source.
   - If unavailable, return without broadcasting.
7. Check source bandwidth.
8. Rent bandwidth through provider chain only if source bandwidth is short.
9. Check source energy.
10. Rent energy through provider chain only if source energy is short.
11. Re-check source resources.
12. Broadcast sweep only when energy and bandwidth deficits are zero.
13. Release provider energy after successful sweep where provider requires release; ProfeeX and re:Fee remain no-op releases.

## Failure Behavior

Payout:

- Estimate unavailable from all providers: transient pre-broadcast failure.
- Bandwidth rental failed before any provider order was accepted: transient pre-broadcast failure.
- Energy rental failed before any provider order was accepted: transient pre-broadcast failure.
- Resource post-check still deficient without an accepted provider order: transient pre-broadcast failure.
- Any accepted or ambiguous provider order failure carries `provider_order_accepted=True` and must not be treated as a clean no-side-effect retry or fallback case.
- No TRC20 broadcast occurs in these cases.

Sweep:

- Same provider/resource failures stop before TRC20 broadcast.
- The source address keeps its token balance.
- Existing scanner/task scheduling can retry later.
- Accepted or ambiguous provider order failures must be logged with accepted-order metadata so follow-up retry/reconciliation can avoid double-renting assumptions.

## Acceptance Criteria

- ProfeeX primary and re:Fee fallback work for payout resource provisioning.
- ProfeeX primary and re:Fee fallback work for sweep resource provisioning.
- ProfeeX estimate failure uses re:Fee `/api/functions/cost/{source_address}` instead of hardcoded energy.
- ProfeeX activation failure uses re:Fee `/api/functions/activate?address=<destination_address>` for payout destination activation fallback.
- re:Fee strict energy acquire can satisfy estimates higher than `REFEE_FIXED_ENERGY_ORDER_AMOUNT`.
- Bandwidth fallback rents on the sender/source address, not destination.
- re:Fee bandwidth minimum `1000` is preserved while callers request the actual transfer need.
- If both providers fail before broadcast, payout retries transiently and sweep does not broadcast.
- Tests cover RED/GREEN for payout and sweep fallback.

# TRON USDT Resource Provider Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one ProfeeX-primary, re:Fee-fallback resource provisioning flow for both TRON USDT payout and TRON USDT sweep.

**Architecture:** Add provider estimate support to re:Fee, then centralize USDT resource quote/provision logic around `source_address -> destination_address`. Payout and sweep will call the same resource pipeline before broadcasting, with payout preserving transient execution retry semantics and sweep returning before broadcast for natural retry.

**Tech Stack:** Python 3.9, unittest, tronpy, requests, pydantic settings, existing `EnergyProvider` and `BandwidthProvider` protocols.

---

## File Structure

- Modify `app/config.py`: replace payout-only fallback config with a generic TRON USDT fallback config and validate re:Fee settings.
- Modify `app/resource_providers/base.py`: add an optional estimate protocol shape through a concrete helper or provider method.
- Modify `app/resource_providers/refee.py`: implement `estimate_usdt_transfer_fee(source_address)`.
- Modify `app/payout_destination_activation.py`: add re:Fee activation fallback behind the same pre-broadcast activation guard.
- Modify `app/resource_providers/factory.py`: keep provider-by-name helpers for energy and bandwidth chains.
- Create or modify `app/usdt_resource_provisioning.py`: shared quote/provisioning logic for payout and sweep.
- Modify `app/payout_resources.py`: make payout quote/ensure a thin wrapper over shared provisioning.
- Modify `app/tasks.py`: route USDT sweep through shared provisioning.
- Modify `app/resource_providers/profeex.py`: expose enough failure classification for fallback decisions.
- Modify `tests/test_refee_energy_accounting.py`: re:Fee estimate and strict energy ordering tests.
- Modify `tests/test_profeex_bandwidth_provider.py`: ProfeeX accepted-order polling and fallback classification tests.
- Modify `tests/test_payout_destination_activation.py`: ProfeeX-to-re:Fee activation fallback tests.
- Modify `tests/test_payout_resources.py`: payout fallback estimate/provisioning tests.
- Modify `tests/test_payout_execution_boundaries.py`: transient pre-broadcast failures remain retryable.
- Modify `tests/test_payout_task_resource_provisioning.py`: sweep fallback tests.
- Modify `tests/test_resource_provider_config.py`: generic fallback config validation tests.

## Task 1: Add re:Fee USDT Energy Estimate

**Files:**
- Modify: `app/resource_providers/refee.py`
- Test: `tests/test_refee_energy_accounting.py`

- [ ] **Step 1: Write failing tests for re:Fee estimate**

Add tests that prove:

```python
def test_refee_provider_estimates_usdt_transfer_fee_from_source_address(self):
    from app.resource_providers.refee import RefeeProvider

    provider = RefeeProvider()
    calls = []

    class Response:
        status_code = 200
        text = '{"cost": 65000}'
        def json(self):
            return {"cost": 65000}

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        return Response()

    original_requests = __import__("app.resource_providers.refee", fromlist=["requests"]).requests
    original_config = __import__("app.resource_providers.refee", fromlist=["config"]).config
    __import__("app.resource_providers.refee", fromlist=["requests"]).requests = SimpleNamespace(get=fake_get)
    __import__("app.resource_providers.refee", fromlist=["config"]).config = SimpleNamespace(
        REFEE=SimpleNamespace(
            api_base_url="https://api.refee.bot",
            api_key=SimpleNamespace(get_secret_value=lambda: "token"),
        )
    )
    try:
        estimate = provider.estimate_usdt_transfer_fee("TSourceAddress")
    finally:
        __import__("app.resource_providers.refee", fromlist=["requests"]).requests = original_requests
        __import__("app.resource_providers.refee", fromlist=["config"]).config = original_config

    self.assertEqual(estimate, {
        "energy_required": 65000,
        "is_new_address": False,
        "trx_burned": None,
        "provider": "refee",
    })
    self.assertEqual(calls[0][0], "https://api.refee.bot/api/functions/cost/TSourceAddress")
    self.assertEqual(calls[0][1]["X-API-Key"], "token")
```

Also add invalid response tests:

```python
def test_refee_provider_rejects_invalid_usdt_transfer_fee_estimate(self):
    from app.resource_providers.refee import RefeeProvider

    provider = RefeeProvider()

    class Response:
        status_code = 200
        text = "{}"

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    for body in ({}, {"cost": "65000"}, {"cost": 0}, {"cost": -1}):
        module = __import__("app.resource_providers.refee", fromlist=["requests"])
        original_requests = module.requests
        original_config = module.config
        module.requests = SimpleNamespace(
            get=lambda url, headers=None, timeout=None, body=body: Response(body)
        )
        module.config = SimpleNamespace(
            REFEE=SimpleNamespace(
                api_base_url="https://api.refee.bot",
                api_key=SimpleNamespace(get_secret_value=lambda: "token"),
            )
        )
        try:
            self.assertIsNone(provider.estimate_usdt_transfer_fee("TSourceAddress"))
        finally:
            module.requests = original_requests
            module.config = original_config
```

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_refee_energy_accounting.RefeeEnergyAccountingTests.test_refee_provider_estimates_usdt_transfer_fee_from_source_address
```

Expected: fail because `RefeeProvider.estimate_usdt_transfer_fee` does not exist.

- [ ] **Step 3: Implement re:Fee estimate**

Add to `RefeeProvider`:

```python
def estimate_usdt_transfer_fee(self, source_address: str) -> dict | None:
    settings = config.REFEE
    if settings is None:
        logger.warning("REFEE config is missing. Cannot estimate USDT fee.")
        return None
    try:
        response = requests.get(
            self._url(settings, f"/api/functions/cost/{source_address}"),
            headers=self._headers(settings),
            timeout=self.REQUEST_TIMEOUT_SEC,
        )
    except requests.RequestException:
        logger.exception("re:Fee USDT fee estimate request failed")
        return None
    if response.status_code != 200:
        logger.warning(
            f"re:Fee USDT fee estimate rejected with status "
            f"{response.status_code}: {response.text}"
        )
        return None
    try:
        data = response.json()
    except ValueError:
        logger.exception("re:Fee USDT fee estimate response is not valid JSON")
        return None
    if not isinstance(data, dict) or type(data.get("cost")) is not int or data["cost"] <= 0:
        logger.warning(f"re:Fee USDT fee estimate has invalid cost: {data}")
        return None
    return {
        "energy_required": data["cost"],
        "is_new_address": False,
        "trx_burned": None,
        "provider": "refee",
    }
```

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
.venv/bin/python -m unittest tests.test_refee_energy_accounting
```

Expected: all tests in module pass.

## Task 2: Fix re:Fee Strict Energy Ordering

**Files:**
- Modify: `app/resource_providers/refee.py`
- Test: `tests/test_refee_energy_accounting.py`

- [ ] **Step 1: Write failing test for strict 131k estimate**

Add:

```python
def test_refee_provider_strict_minimum_orders_above_fixed_amount(self):
    from app.resource_providers.refee import RefeeProvider

    provider = RefeeProvider(tron_client=FakeTronClient([0, 131000]))
    created_orders = []
    provider._create_order = lambda settings, receiver, amount, **kwargs: created_orders.append(
        (receiver, amount)
    ) or {"id": "order-1", "status": "pending"}
    provider._wait_until_delegated = lambda settings, order_id, order: {
        "id": order_id,
        "status": "delegated",
    }

    module = __import__("app.resource_providers.refee", fromlist=["config"])
    original_config = module.config
    module.config = SimpleNamespace(
        REFEE=SimpleNamespace(
            rent_duration_label="1h",
            energy_overprovision_factor=Decimal("1.05"),
            min_energy_order_amount=30000,
            api_base_url="https://api.refee.bot",
            api_key=SimpleNamespace(get_secret_value=lambda: "token"),
            poll_interval_sec=0.01,
            timeout_sec=1,
        ),
        REFEE_FIXED_ENERGY_ORDER_AMOUNT=65000,
    )
    try:
        acquired = provider.acquire_energy(
            ONETIME,
            131000,
            {},
            minimum_energy_required=131000,
            strict_minimum_required=True,
        )
    finally:
        module.config = original_config

    self.assertTrue(acquired)
    self.assertEqual(created_orders, [(ONETIME, 131000)])
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_refee_energy_accounting.RefeeEnergyAccountingTests.test_refee_provider_strict_minimum_orders_above_fixed_amount
```

Expected: fail because current code orders fixed `65000`.

- [ ] **Step 3: Implement strict order amount**

Change the fixed-order branch in `RefeeProvider.acquire_energy` so strict minimum can raise the requested order amount:

```python
if fixed_order_amount > 0:
    requested_amount = fixed_order_amount
    if strict_minimum_required and minimum_energy_required is not None:
        missing_for_minimum = max(minimum_energy_required - onetime_energy_available, 0)
        requested_amount = max(requested_amount, missing_for_minimum)
else:
    energy_to_provision = energy_required - onetime_energy_available
    requested_amount = int(
        (
            Decimal(energy_to_provision) * settings.energy_overprovision_factor
        ).to_integral_value(rounding=ROUND_CEILING)
    )
```

- [ ] **Step 4: Run re:Fee tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_refee_energy_accounting tests.test_refee_bandwidth_guard
```

Expected: pass.

## Task 3: Add re:Fee Destination Activation Fallback

**Files:**
- Modify: `app/resource_providers/refee.py`
- Modify: `app/payout_destination_activation.py`
- Test: `tests/test_payout_destination_activation.py`

- [ ] **Step 1: Write failing tests for re:Fee activation**

Add tests that prove:

```python
def test_refee_activation_posts_destination_address(self):
    from app.resource_providers.refee import RefeeProvider

    provider = RefeeProvider()
    calls = []

    class Response:
        status_code = 200
        text = '{"txn_hash":"activation-tx"}'

        def json(self):
            return {
                "id": "tx-id",
                "txn_hash": "activation-tx",
                "order_id": None,
                "address_from": None,
                "address_to": DESTINATION,
                "operation": "transfer",
                "service": "activation",
                "resource": None,
                "amount": 0,
                "amount_sun": 0,
                "cost": 0,
                "tariff": None,
                "order": None,
                "created_at": "2026-06-13T00:00:00Z",
            }

    def fake_post(url, params=None, headers=None, timeout=None):
        calls.append((url, params, headers, timeout))
        return Response()

    module = __import__("app.resource_providers.refee", fromlist=["requests"])
    original_requests = module.requests
    original_config = module.config
    module.requests = SimpleNamespace(post=fake_post)
    module.config = SimpleNamespace(
        REFEE=SimpleNamespace(
            api_base_url="https://api.refee.bot",
            api_key=SimpleNamespace(get_secret_value=lambda: "token"),
        )
    )
    try:
        order = provider.activate_address(DESTINATION)
    finally:
        module.requests = original_requests
        module.config = original_config

    self.assertEqual(order["txn_hash"], "activation-tx")
    self.assertEqual(calls[0][0], "https://api.refee.bot/api/functions/activate")
    self.assertEqual(calls[0][1], {"address": DESTINATION})
    self.assertEqual(calls[0][2]["X-API-Key"], "token")
```

Add:

```python
def test_refee_activation_400_means_already_active(self):
    from app.resource_providers.refee import RefeeProvider

    provider = RefeeProvider()

    class Response:
        status_code = 400
        text = "The wallet does not require activation"

    def fake_post(url, params=None, headers=None, timeout=None):
        return Response()

    module = __import__("app.resource_providers.refee", fromlist=["requests"])
    original_requests = module.requests
    original_config = module.config
    module.requests = SimpleNamespace(post=fake_post)
    module.config = SimpleNamespace(
        REFEE=SimpleNamespace(
            api_base_url="https://api.refee.bot",
            api_key=SimpleNamespace(get_secret_value=lambda: "token"),
        )
    )
    try:
        order = provider.activate_address(DESTINATION)
    finally:
        module.requests = original_requests
        module.config = original_config

    self.assertEqual(order, {"status": "already_active", "address": DESTINATION})
```

```python
def test_refee_activation_402_is_retryable_provider_error(self):
    from app.resource_providers.refee import RefeeProvider, RefeeProviderError

    provider = RefeeProvider()

    class Response:
        status_code = 402
        text = "Insufficient funds in the balance"

    def fake_post(url, params=None, headers=None, timeout=None):
        return Response()

    module = __import__("app.resource_providers.refee", fromlist=["requests"])
    original_requests = module.requests
    original_config = module.config
    module.requests = SimpleNamespace(post=fake_post)
    module.config = SimpleNamespace(
        REFEE=SimpleNamespace(
            api_base_url="https://api.refee.bot",
            api_key=SimpleNamespace(get_secret_value=lambda: "token"),
        )
    )
    try:
        with self.assertRaises(RefeeProviderError) as cm:
            provider.activate_address(DESTINATION)
    finally:
        module.requests = original_requests
        module.config = original_config

    self.assertEqual(cm.exception.error_code, "INSUFFICIENT_BALANCE")
    self.assertTrue(cm.exception.temporary)
```

```python
def test_refee_activation_401_is_terminal_provider_error(self):
    from app.resource_providers.refee import RefeeProvider, RefeeProviderError

    provider = RefeeProvider()

    class Response:
        status_code = 401
        text = "Invalid API key or it was not transmitted"

    def fake_post(url, params=None, headers=None, timeout=None):
        return Response()

    module = __import__("app.resource_providers.refee", fromlist=["requests"])
    original_requests = module.requests
    original_config = module.config
    module.requests = SimpleNamespace(post=fake_post)
    module.config = SimpleNamespace(
        REFEE=SimpleNamespace(
            api_base_url="https://api.refee.bot",
            api_key=SimpleNamespace(get_secret_value=lambda: "token"),
        )
    )
    try:
        with self.assertRaises(RefeeProviderError) as cm:
            provider.activate_address(DESTINATION)
    finally:
        module.requests = original_requests
        module.config = original_config

    self.assertEqual(cm.exception.error_code, "CONFIGURATION_ERROR")
    self.assertFalse(cm.exception.temporary)
```

- [ ] **Step 2: Write failing payout activation fallback test**

Add:

```python
def test_destination_activation_falls_back_to_refee_when_profeex_unavailable(self):
    profeex_provider.activate_address.side_effect = DestinationActivationError(
        "ProfeeX activation unavailable",
        code="PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE",
        temporary=True,
    )
    refee_provider.activate_address.return_value = {
        "txn_hash": "activation-tx",
        "address_to": DESTINATION,
    }

    result = ensure_destination_activated(
        DESTINATION,
        quote_fn=lambda receiver: {
            "energy_required": 65000,
            "is_new_address": True,
            "trx_burned": "1.1",
        },
        activation_providers=[("profeex", profeex_provider), ("refee", refee_provider)],
    )

    self.assertEqual(result["provider"], "refee")
    self.assertEqual(result["txn_hash"], "activation-tx")
    self.assertEqual(profeex_provider.activate_address.call_args[0][0], DESTINATION)
    self.assertEqual(refee_provider.activate_address.call_args[0][0], DESTINATION)
```

Add:

```python
def test_destination_activation_retries_when_profeex_and_refee_unavailable(self):
    profeex_provider.activate_address.side_effect = DestinationActivationError(
        "ProfeeX activation unavailable",
        code="PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE",
        temporary=True,
    )
    refee_provider.activate_address.side_effect = DestinationActivationError(
        "re:Fee activation unavailable",
        code="PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE",
        temporary=True,
    )

    with self.assertRaises(DestinationActivationError) as cm:
        ensure_destination_activated(
            DESTINATION,
            quote_fn=lambda receiver: {
                "energy_required": 65000,
                "is_new_address": True,
                "trx_burned": "1.1",
            },
            activation_providers=[("profeex", profeex_provider), ("refee", refee_provider)],
        )

    self.assertEqual(cm.exception.code, "PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE")
    self.assertTrue(cm.exception.temporary)
```

- [ ] **Step 3: Run tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_payout_destination_activation
```

Expected: fail until re:Fee activation provider and activation chain exist.

- [ ] **Step 4: Implement re:Fee activation endpoint**

Add to `RefeeProvider`:

```python
class RefeeProviderError(RuntimeError):
    def __init__(self, resource_name, message, error_code=None, temporary=False):
        super().__init__(message)
        self.resource_name = resource_name
        self.error_code = error_code
        self.temporary = temporary


def activate_address(self, destination: str) -> dict | None:
    settings = config.REFEE
    if settings is None:
        raise RefeeProviderError(
            "activation",
            "REFEE config is missing. Cannot activate destination.",
            "CONFIGURATION_ERROR",
            temporary=False,
        )
    try:
        response = requests.post(
            self._url(settings, "/api/functions/activate"),
            params={"address": destination},
            headers=self._headers(settings),
            timeout=self.REQUEST_TIMEOUT_SEC,
        )
    except requests.RequestException as exc:
        raise RefeeProviderError(
            "activation",
            f"re:Fee activation request failed: {exc}",
            "SERVICE_UNAVAILABLE",
            temporary=True,
        ) from exc
    if response.status_code == 400:
        return {"status": "already_active", "address": destination}
    if response.status_code in {408, 429} or 500 <= response.status_code <= 599:
        raise RefeeProviderError(
            "activation",
            f"re:Fee activation unavailable: {response.text}",
            "SERVICE_UNAVAILABLE",
            temporary=True,
        )
    if response.status_code == 402:
        raise RefeeProviderError(
            "activation",
            f"re:Fee activation has insufficient balance: {response.text}",
            "INSUFFICIENT_BALANCE",
            temporary=True,
        )
    if response.status_code in {401, 403, 422}:
        raise RefeeProviderError(
            "activation",
            f"re:Fee activation rejected with status {response.status_code}: {response.text}",
            "CONFIGURATION_ERROR",
            temporary=False,
        )
    if response.status_code != 200:
        raise RefeeProviderError(
            "activation",
            f"re:Fee activation rejected with status {response.status_code}: {response.text}",
            "UNKNOWN_ERROR",
            temporary=True,
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise RefeeProviderError(
            "activation",
            "re:Fee activation response is not valid JSON",
            "SCHEMA_ERROR",
            temporary=False,
        ) from exc
    if not isinstance(data, dict) or not data.get("txn_hash"):
        raise RefeeProviderError(
            "activation",
            f"re:Fee activation response has no txn_hash: {data}",
            "SCHEMA_ERROR",
            temporary=False,
        )
    return data
```

- [ ] **Step 5: Implement activation provider chain**

In `app/payout_destination_activation.py`, allow injection of activation providers for tests and default to:

```python
activation_providers = [
    ("profeex", ProfeeXProvider()),
]
if config.TRON_USDT_RESOURCE_FALLBACK_PROVIDER == "refee":
    activation_providers.append(("refee", RefeeProvider()))
```

When ProfeeX activation fails with a temporary/operational error, try re:Fee. If re:Fee succeeds, persist activation record with provider metadata. If re:Fee raises `RefeeProviderError`, convert it to `DestinationActivationError` with the same `temporary` flag. That means re:Fee `402` is retryable, while `401`, `403`, and `422` are terminal configuration/provider errors.

- [ ] **Step 6: Run activation tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_payout_destination_activation
```

Expected: pass.

## Task 4: Add Shared USDT Resource Provisioning

**Files:**
- Create: `app/usdt_resource_provisioning.py`
- Modify: `app/resource_providers/factory.py`
- Test: `tests/test_usdt_resource_provisioning.py`

- [ ] **Step 1: Write failing tests for shared quote and provider chain**

Create `tests/test_usdt_resource_provisioning.py` with fixtures:

```python
SOURCE = "TSourceAddress"
DESTINATION = "TDestinationAddress"

class FakeClient:
    def __init__(self, resources):
        self.resources = list(resources)

    def get_account_resource(self, address):
        if len(self.resources) > 1:
            return self.resources.pop(0)
        return self.resources[0]

class RecordingEnergyProvider:
    def __init__(self, result):
        self.result = result
        self.acquire_calls = []

    def acquire_energy(self, *args, **kwargs):
        self.acquire_calls.append((args, kwargs))
        return self.result

class RecordingBandwidthProvider:
    def __init__(self, result):
        self.result = result
        self.acquire_calls = []

    def acquire_bandwidth(self, *args, **kwargs):
        self.acquire_calls.append((args, kwargs))
        return self.result
```

Add these concrete tests:

```python
def test_estimate_chain_falls_back_from_profeex_to_refee(self):
    quote = estimate_usdt_transfer_resources(
        SOURCE,
        DESTINATION,
        Decimal("1.25"),
        tron_client=FakeClient([{
            "EnergyLimit": 0,
            "EnergyUsed": 0,
            "freeNetLimit": 600,
            "freeNetUsed": 0,
            "NetLimit": 0,
            "NetUsed": 0,
        }]),
    )
    self.assertEqual(quote.energy.required, 65000)
    self.assertEqual(quote.estimate_provider, "refee")

def test_provision_rents_bandwidth_on_source_address_only_when_short(self):
    primary = RecordingBandwidthProvider(False)
    fallback = RecordingBandwidthProvider(True)
    quote = ensure_usdt_transfer_resources(
        SOURCE,
        DESTINATION,
        Decimal("1.25"),
        tron_client=FakeClient([
            {"EnergyLimit": 65000, "EnergyUsed": 0, "freeNetLimit": 600, "freeNetUsed": 600, "NetLimit": 0, "NetUsed": 0},
            {"EnergyLimit": 65000, "EnergyUsed": 0, "freeNetLimit": 600, "freeNetUsed": 600, "NetLimit": 1000, "NetUsed": 0},
        ]),
    )
    self.assertEqual(quote.bandwidth.deficit, 0)
    self.assertEqual(primary.acquire_calls, [((SOURCE, 346), {})])
    self.assertEqual(fallback.acquire_calls, [((SOURCE, 346), {})])

def test_provision_rents_energy_on_source_address_only_when_short(self):
    primary = RecordingEnergyProvider(False)
    fallback = RecordingEnergyProvider(True)
    quote = ensure_usdt_transfer_resources(
        SOURCE,
        DESTINATION,
        Decimal("1.25"),
        tron_client=FakeClient([
            {"EnergyLimit": 0, "EnergyUsed": 0, "freeNetLimit": 600, "freeNetUsed": 0, "NetLimit": 0, "NetUsed": 0},
            {"EnergyLimit": 0, "EnergyUsed": 0, "freeNetLimit": 600, "freeNetUsed": 0, "NetLimit": 0, "NetUsed": 0},
            {"EnergyLimit": 65000, "EnergyUsed": 0, "freeNetLimit": 600, "freeNetUsed": 0, "NetLimit": 0, "NetUsed": 0},
        ]),
    )
    self.assertEqual(quote.energy.deficit, 0)
    self.assertEqual(primary.acquire_calls[0][0][0], SOURCE)
    self.assertEqual(fallback.acquire_calls[0][0][0], SOURCE)
    self.assertEqual(fallback.acquire_calls[0][1]["minimum_energy_required"], 65000)

def test_provision_returns_temporary_error_when_all_estimates_fail(self):
    with self.assertRaises(UsdtResourceError) as cm:
        ensure_usdt_transfer_resources(
            SOURCE,
            DESTINATION,
            Decimal("1.25"),
            tron_client=FakeClient([{
                "EnergyLimit": 0,
                "EnergyUsed": 0,
                "freeNetLimit": 600,
                "freeNetUsed": 0,
                "NetLimit": 0,
                "NetUsed": 0,
            }]),
        )
    self.assertEqual(cm.exception.code, "RESOURCE_ESTIMATE_UNAVAILABLE")
    self.assertTrue(cm.exception.temporary)
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_usdt_resource_provisioning
```

Expected: fail because module does not exist.

- [ ] **Step 3: Implement shared dataclasses and chains**

Create:

```python
@dataclass
class UsdtResourceReadiness:
    provider: str | None
    required: int
    available: int
    deficit: int

@dataclass
class UsdtResourceQuote:
    source_address: str
    destination: str
    amount: str
    estimate_provider: str | None
    activation_required: bool
    estimated_trx_burned: str | None
    energy: UsdtResourceReadiness
    bandwidth: UsdtResourceReadiness
    submit_ready: bool
    blocking_code: str | None
    blocking_reason: str | None

class UsdtResourceError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None, temporary: bool = False):
        super().__init__(message)
        self.code = code
        self.temporary = temporary
```

Add helpers:

```python
def estimate_usdt_transfer_fee_chain(source_address, destination, tron_client=None):
    estimate = ProfeeXProvider().estimate_usdt_transfer_fee(destination)
    if estimate is not None:
        return "profeex", estimate
    if _fallback_provider_name() == "refee":
        estimate = RefeeProvider(tron_client=tron_client).estimate_usdt_transfer_fee(
            source_address
        )
        if estimate is not None:
            return "refee", estimate
    return None, None

def estimate_usdt_transfer_resources(source_address, destination, amount, tron_client=None):
    client = tron_client or ConnectionManager.client()
    account_resource = _get_account_resources(client, source_address)
    estimate_provider, fee_estimate = estimate_usdt_transfer_fee_chain(
        source_address,
        destination,
        tron_client=client,
    )
    return _quote_from_resources(
        source_address,
        destination,
        amount,
        account_resource,
        estimate_provider,
        fee_estimate,
        tron_client=client,
    )

def ensure_usdt_transfer_resources(source_address, destination, amount, tron_client=None):
    quote = estimate_usdt_transfer_resources(
        source_address,
        destination,
        amount,
        tron_client=tron_client,
    )
    if not quote.submit_ready:
        raise UsdtResourceError(
            quote.blocking_reason or "TRON USDT resources are not ready",
            code=quote.blocking_code,
            temporary=quote.blocking_code in TEMPORARY_RESOURCE_BLOCKING_CODES,
        )
    return _provision_and_recheck(quote, tron_client=tron_client)
```

- [ ] **Step 4: Run shared provisioning tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_usdt_resource_provisioning
```

Expected: pass.

## Task 5: Classify ProfeeX Fallback Eligibility

**Files:**
- Modify: `app/resource_providers/profeex.py`
- Test: `tests/test_profeex_bandwidth_provider.py`

- [ ] **Step 1: Write failing tests for fallback eligibility**

Add tests that prove:

```python
def test_profeex_network_timeout_is_fallback_eligible(self):
    from app.resource_providers.profeex import classify_profeex_failure

    failure = classify_profeex_failure("REQUEST_TIMEOUT")

    self.assertEqual(failure.code, "REQUEST_TIMEOUT")
    self.assertTrue(failure.temporary)
    self.assertTrue(failure.fallback_eligible)
    self.assertFalse(failure.order_accepted)
```

```python
def test_profeex_insufficient_balance_is_fallback_eligible(self):
    from app.resource_providers.profeex import classify_profeex_failure

    failure = classify_profeex_failure("INSUFFICIENT_BALANCE")

    self.assertEqual(failure.code, "INSUFFICIENT_BALANCE")
    self.assertTrue(failure.temporary)
    self.assertTrue(failure.fallback_eligible)
```

```python
def test_profeex_invalid_address_is_not_fallback_eligible(self):
    from app.resource_providers.profeex import classify_profeex_failure

    failure = classify_profeex_failure("INVALID_ADDRESS")

    self.assertEqual(failure.code, "INVALID_ADDRESS")
    self.assertFalse(failure.temporary)
    self.assertFalse(failure.fallback_eligible)
```

```python
def test_profeex_malformed_pre_accept_response_is_fallback_eligible(self):
    from app.resource_providers.profeex import classify_profeex_failure

    failure = classify_profeex_failure("MALFORMED_PRE_ACCEPT_RESPONSE")

    self.assertEqual(failure.code, "MALFORMED_PRE_ACCEPT_RESPONSE")
    self.assertTrue(failure.temporary)
    self.assertTrue(failure.fallback_eligible)
```

```python
def test_profeex_accepted_response_without_task_id_is_not_fallback_eligible(self):
    from app.resource_providers.profeex import classify_profeex_failure

    failure = classify_profeex_failure(
        "ACCEPTED_ORDER_WITHOUT_TASK_ID",
        order_accepted=True,
    )

    self.assertEqual(failure.code, "ACCEPTED_ORDER_WITHOUT_TASK_ID")
    self.assertTrue(failure.temporary)
    self.assertTrue(failure.order_accepted)
    self.assertFalse(failure.fallback_eligible)
```

```python
def test_profeex_accepted_order_is_polled_before_refee_fallback(self):
    from app.resource_providers.profeex import classify_profeex_failure

    failure = classify_profeex_failure(
        "POLL_TEMPORARY_ERROR",
        task_id="task-1",
        order_accepted=True,
    )

    self.assertEqual(failure.task_id, "task-1")
    self.assertTrue(failure.temporary)
    self.assertTrue(failure.order_accepted)
    self.assertFalse(failure.fallback_eligible)
```

```python
def test_profeex_accepted_order_timeout_is_not_refee_fallback_eligible(self):
    from app.resource_providers.profeex import classify_profeex_failure

    failure = classify_profeex_failure(
        "ORDER_TIMEOUT",
        task_id="task-1",
        order_accepted=True,
    )

    self.assertEqual(failure.task_id, "task-1")
    self.assertTrue(failure.temporary)
    self.assertTrue(failure.order_accepted)
    self.assertFalse(failure.fallback_eligible)
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_profeex_bandwidth_provider
```

Expected: fail until ProfeeX failure classification is exposed to shared provisioning.

- [ ] **Step 3: Implement fallback classification**

Introduce a small result object that preserves reason:

```python
from dataclasses import dataclass


@dataclass
class ProviderFailure:
    code: str
    temporary: bool
    fallback_eligible: bool
    order_accepted: bool = False
    task_id: str | None = None
```

Classify ProfeeX failures as fallback-eligible only when no actual resource rental has been confirmed or ambiguously accepted. Keep this broad enough to cover provider outages, timeouts, temporary API failures, malformed pre-acceptance responses, and insufficient ProfeeX balance before order acceptance:

```python
FALLBACK_ELIGIBLE_PROFEEX_CODES = {
    "NETWORK_ERROR",
    "REQUEST_TIMEOUT",
    "CONNECT_ERROR",
    "READ_ERROR",
    "SERVICE_UNAVAILABLE",
    "HTTP_408",
    "HTTP_429",
    "RATE_LIMIT_EXCEEDED",
    "INSUFFICIENT_BALANCE",
    "MALFORMED_PRE_ACCEPT_RESPONSE",
}
```

Classify these as not fallback-eligible because they point to local/integration problems or ambiguous accepted-order state rather than a safe provider fallback:

```python
NON_FALLBACK_PROFEEX_CODES = {
    "INVALID_ADDRESS",
    "INVALID_PARAMETERS",
    "AUTHORIZATION_ERROR",
    "IP_NOT_WHITELISTED",
    "CONFIGURATION_ERROR",
    "ACCEPTED_ORDER_WITHOUT_TASK_ID",
}
```

Accepted-order failures are always non-fallback, even if the same code would be
retryable before acceptance:

```python
ACCEPTED_ORDER_NON_FALLBACK_PROFEEX_CODES = {
    "ORDER_TIMEOUT",
    "ORDER_FAILED",
    "ORDER_CANCELLED",
    "PROCESSING_FAILED",
    "RESOURCE_READ_FAILED",
    "RESOURCE_RECHECK_FAILED",
    "ACCEPTED_ORDER_WITHOUT_TASK_ID",
}
```

Add this classifier:

```python
def classify_profeex_failure(
    code: str,
    *,
    task_id: str | None = None,
    order_accepted: bool = False,
) -> ProviderFailure:
    if order_accepted:
        return ProviderFailure(
            code=code,
            temporary=code not in {
                "INVALID_ADDRESS",
                "INVALID_PARAMETERS",
                "AUTHORIZATION_ERROR",
                "IP_NOT_WHITELISTED",
                "CONFIGURATION_ERROR",
            },
            fallback_eligible=False,
            order_accepted=True,
            task_id=task_id,
        )

    if code in FALLBACK_ELIGIBLE_PROFEEX_CODES:
        return ProviderFailure(
            code=code,
            temporary=True,
            fallback_eligible=True,
            order_accepted=order_accepted,
            task_id=task_id,
        )

    if code in NON_FALLBACK_PROFEEX_CODES:
        return ProviderFailure(
            code=code,
            temporary=False,
            fallback_eligible=False,
            order_accepted=order_accepted,
            task_id=task_id,
        )

    return ProviderFailure(
        code=code,
        temporary=True,
        fallback_eligible=not order_accepted,
        order_accepted=order_accepted,
        task_id=task_id,
    )
```

Wire `_create_order`, `_wait_until_active`, `acquire_energy`, and `acquire_bandwidth` to use this classifier when they need to expose a provider failure to shared provisioning. If ProfeeX returns an accepted order with `task_id`, or an accepted-looking response without a usable id, treat it as potentially rented. Do not fallback to re:Fee after any accepted or ambiguous order signal, including transient poll misses, terminal order failure, polling timeout, or final on-chain resource recheck failure.

- [ ] **Step 4: Run ProfeeX tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_profeex_bandwidth_provider
```

Expected: pass.

## Task 6: Rewire Payout to Shared Provisioning

**Files:**
- Modify: `app/payout_resources.py`
- Modify: `app/payout_execution.py`
- Test: `tests/test_payout_resources.py`
- Test: `tests/test_payout_execution_boundaries.py`

- [ ] **Step 1: Replace hardcoded 131k tests**

Change the payout test that expects `131000` fallback to expect re:Fee estimate:

```python
def test_quote_uses_refee_estimate_when_profeex_estimate_fails_with_refee_fallback(self):
    quote = payout_resources.estimate_fee_deposit_resources_for_usdt_payout(
        DESTINATION,
        Decimal("1.25"),
        tron_client=client,
    )
    self.assertTrue(quote.submit_ready)
    self.assertEqual(quote.energy.required, 65000)
    self.assertEqual(quote.energy.deficit, 65000)
    self.assertEqual(quote.estimate_provider, "refee")
```

Add:

```python
def test_quote_blocks_temporarily_when_profeex_and_refee_estimates_fail(self):
    quote = payout_resources.estimate_fee_deposit_resources_for_usdt_payout(
        DESTINATION,
        Decimal("1.25"),
        tron_client=client,
    )
    self.assertFalse(quote.submit_ready)
    self.assertEqual(quote.blocking_code, "RESOURCE_ESTIMATE_UNAVAILABLE")
```

- [ ] **Step 2: Run payout tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_payout_resources tests.test_payout_execution_boundaries
```

Expected: fail while payout wrapper still uses hardcoded conservative estimate.

- [ ] **Step 3: Make payout wrapper call shared module**

In `app/payout_resources.py`:

```python
from app.usdt_resource_provisioning import (
    UsdtResourceError,
    estimate_usdt_transfer_resources,
    ensure_usdt_transfer_resources,
)

def estimate_fee_deposit_resources_for_usdt_payout(destination, amount, *, tron_client=None):
    _, fee_deposit_address = get_key(KeyType.fee_deposit)
    quote = estimate_usdt_transfer_resources(
        fee_deposit_address,
        destination,
        amount,
        tron_client=tron_client,
    )
    return convert_shared_quote_to_existing_payout_quote(quote)

def ensure_fee_deposit_resources_for_usdt_payout(destination, amount, *, tron_client=None, allow_destination_activation=False):
    _, fee_deposit_address = get_key(KeyType.fee_deposit)
    try:
        quote = ensure_usdt_transfer_resources(
            fee_deposit_address,
            destination,
            amount,
            tron_client=tron_client,
        )
    except UsdtResourceError as exc:
        raise PayoutResourceError(str(exc), code=exc.code, temporary=exc.temporary) from exc
    return convert_shared_quote_to_existing_payout_quote(quote)
```

Keep existing destination activation behavior before calling shared ensure when `DESTINATION_NOT_ACTIVATED` is detected.

When ProfeeX estimate is unavailable and re:Fee estimate succeeds, explicitly check payout destination activation through TRON account state before resource rental. A re:Fee energy estimate is not activation proof because `/api/functions/cost/{address}` estimates source energy only.

- [ ] **Step 4: Update retryable payout codes**

In `app/payout_execution.py`, include:

```python
"RESOURCE_ESTIMATE_UNAVAILABLE",
"PROVIDER_FAILED",
"RESOURCE_RECHECK_FAILED",
```

in `RETRYABLE_TEMPORARY_PRE_BROADCAST_ERROR_CODES`.

- [ ] **Step 5: Run payout tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_payout_resources tests.test_payout_execution_boundaries
```

Expected: pass.

## Task 7: Rewire Sweep to Shared Provisioning

**Files:**
- Modify: `app/tasks.py`
- Test: `tests/test_payout_task_resource_provisioning.py`

- [ ] **Step 1: Write failing sweep fallback tests**

Add tests:

```python
def test_sweep_falls_back_to_refee_estimate_energy_and_bandwidth_when_profeex_unavailable(self):
    result = transfer_trc20_from(ONETIME, "USDT")
    self.assertIsInstance(result, dict)
    self.assertEqual(profeex_bandwidth.acquire_calls, [((ONETIME, 346), {})])
    self.assertEqual(refee_bandwidth.acquire_calls, [((ONETIME, 346), {})])
    self.assertEqual(profeex_energy.acquire_calls[0][0][0], ONETIME)
    self.assertEqual(refee_energy.acquire_calls[0][0][0], ONETIME)
    self.assertEqual(refee_energy.acquire_calls[0][1]["minimum_energy_required"], 65000)
    self.assertEqual(fake_contract.transfer_calls, [(FEE_DEPOSIT, token_balance)])
```

```python
def test_sweep_does_not_broadcast_when_all_resource_estimates_fail(self):
    result = transfer_trc20_from(ONETIME, "USDT")
    self.assertFalse(result)
    self.assertEqual(fake_contract.transfer_calls, [])
```

```python
def test_sweep_rents_bandwidth_on_onetime_address_not_fee_wallet(self):
    transfer_trc20_from(ONETIME, "USDT")
    self.assertEqual(refee_bandwidth.acquire_calls[0][0][0], ONETIME)
    self.assertNotEqual(refee_bandwidth.acquire_calls[0][0][0], FEE_DEPOSIT)
```

- [ ] **Step 2: Run sweep tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_payout_task_resource_provisioning
```

Expected: fail because sweep still uses `estimate_trc20_sweep_energy` and single configured providers.

- [ ] **Step 3: Replace sweep resource preparation**

In `transfer_trc20_from`, after source activation and before token transfer:

```python
if symbol == "USDT" and use_external_energy_provider:
    try:
        ensure_usdt_transfer_resources(
            onetime_publ_key,
            main_publ_key,
            balance,
            tron_client=tron_client,
        )
    except UsdtResourceError as exc:
        logger.warning(
            "USDT sweep resources are not ready: code=%s temporary=%s message=%s",
            exc.code,
            exc.temporary,
            exc,
        )
        return False
```

Leave the existing staking and TRX-burn branch in `transfer_trc20_from` unchanged for the non-external-provider path.

Remove the re:Fee fixed estimate branch from `estimate_trc20_sweep_energy` for the external ProfeeX/re:Fee path.

- [ ] **Step 4: Run sweep tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_payout_task_resource_provisioning
```

Expected: pass.

## Task 8: Config and Docs

**Files:**
- Modify: `app/config.py`
- Modify: `README.md`
- Modify: `docs/DEPLOYMENT.md`
- Test: `tests/test_resource_provider_config.py`

- [ ] **Step 1: Write config validation tests**

Add:

```python
def test_refee_required_for_tron_usdt_resource_fallback_provider(self):
    with self.assertRaises(ValueError):
        Settings(TRON_USDT_RESOURCE_FALLBACK_PROVIDER="refee", REFEE=None)
```

```python
def test_refee_tron_usdt_resource_fallback_provider_is_valid_when_configured(self):
    settings = Settings(
        TRON_USDT_RESOURCE_FALLBACK_PROVIDER="refee",
        REFEE={"api_key": "token"},
    )
    self.assertEqual(settings.TRON_USDT_RESOURCE_FALLBACK_PROVIDER, "refee")
```

- [ ] **Step 2: Run config tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_resource_provider_config
```

Expected: fail until generic config exists.

- [ ] **Step 3: Add generic fallback config**

In `Settings`:

```python
TRON_USDT_RESOURCE_FALLBACK_PROVIDER: Literal["disabled", "refee"] = "disabled"
```

In `validate_resource_provider_config_state`:

```python
if self.TRON_USDT_RESOURCE_FALLBACK_PROVIDER == "refee" and self.REFEE is None:
    raise ValueError(
        "REFEE must be configured when "
        "TRON_USDT_RESOURCE_FALLBACK_PROVIDER='refee'"
    )
```

Remove the incomplete payout-only `PAYOUT_RESOURCE_FALLBACK_PROVIDER` config from the uncommitted patch.

- [ ] **Step 4: Update docs**

Document:

```env
ENERGY_PROVIDER=profeex
BANDWIDTH_PROVIDER=profeex
TRON_USDT_RESOURCE_FALLBACK_PROVIDER=refee
REFEE='{"api_key":"REPLACE_WITH_REFEE_API_KEY","api_base_url":"https://api.refee.bot"}'
PROFEEX='{"api_key":"REPLACE_WITH_PROFEEX_API_KEY"}'
```

Document that:

- ProfeeX estimate is tried first.
- re:Fee estimate uses `/api/functions/cost/{source_address}`.
- No hardcoded `131000` fallback is used.
- re:Fee bandwidth minimum remains `1000`.

- [ ] **Step 5: Run config and doc-adjacent tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_resource_provider_config
```

Expected: pass.

## Task 9: Final Verification

**Files:**
- No direct edits.

- [ ] **Step 1: Run targeted test suite**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_usdt_resource_provisioning \
  tests.test_payout_resources \
  tests.test_payout_execution_boundaries \
  tests.test_payout_task_resource_provisioning \
  tests.test_refee_energy_accounting \
  tests.test_refee_bandwidth_guard \
  tests.test_profeex_bandwidth_provider \
  tests.test_resource_provider_config
```

Expected: all targeted tests pass.

- [ ] **Step 2: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output and exit code `0`.

- [ ] **Step 3: Document full discovery caveat**

Run full discovery only if local Redis and local HTTP dependencies are available:

```bash
.venv/bin/python -m unittest discover tests
```

Expected in sandbox: may fail on `localhost:6379` Redis and local HTTP permissions. If it fails only for those infrastructure reasons, record the exact failure summary and do not claim full discovery passed.

## Self-Review

Spec coverage:

- Payout and sweep are both covered by Tasks 6 and 7.
- Payout destination activation fallback is covered by Task 3.
- ProfeeX primary and re:Fee resource fallback chains are covered by Tasks 4 and 5.
- re:Fee estimate endpoint is covered by Task 1.
- Removal of hardcoded `131000` is covered by Tasks 1, 2, 4, 6, and 7.
- Bandwidth minimum `1000` preservation is covered by Task 4 and existing re:Fee bandwidth tests.
- Transient pre-broadcast payout behavior is covered by Task 6.

Placeholder scan:

- The plan contains no open placeholder markers.
- Each implementation task has concrete files, tests, commands, and expected results.

Type consistency:

- Shared names use `UsdtResourceQuote`, `UsdtResourceReadiness`, and `UsdtResourceError`.
- Config uses `TRON_USDT_RESOURCE_FALLBACK_PROVIDER` consistently.

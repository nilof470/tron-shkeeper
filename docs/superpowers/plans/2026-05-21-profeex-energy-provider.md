# ProfeeX Energy Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ProfeeX as a configurable TRON energy rental provider while preserving existing ProfeeX bandwidth rental and re:Fee behavior.

**Architecture:** Extend the existing provider abstraction instead of adding a new sweep path. ProfeeX becomes a dual-capability provider with shared order creation and polling helpers for energy and bandwidth. `tasks.py` treats `refee` and `profeex` as external energy providers, while staking remains the local delegation path.

**Tech Stack:** Python 3.12/3.13, Pydantic v2 settings, `requests`, Celery task code, `unittest`, existing TRON helper utilities.

---

## File Structure

- Modify `app/profeex.py`: add ProfeeX energy config fields, fixed order fields, and provider API limit validation.
- Modify `app/config.py`: allow `ENERGY_PROVIDER="profeex"` and require `PROFEEX` for ProfeeX energy.
- Modify `app/resource_providers/profeex.py`: turn the provider into a shared energy/bandwidth implementation with `release_energy()`.
- Modify `app/resource_providers/factory.py`: return ProfeeX for energy provider selection.
- Modify `app/resource_providers/__init__.py`: export the generalized ProfeeX provider while keeping the current bandwidth class alias.
- Modify `app/energy_provider.py`: export the generalized ProfeeX provider alias for compatibility with existing provider imports.
- Modify `app/tasks.py`: add external-provider control flow for ProfeeX without broadening re:Fee burn fallback.
- Modify `tests/test_resource_provider_config.py`: config validation coverage.
- Modify `tests/test_resource_provider_factory.py`: factory coverage.
- Modify `tests/test_profeex_bandwidth_provider.py`: ProfeeX energy and updated bandwidth provider behavior.
- Modify `tests/test_refee_bandwidth_guard.py`: task-level sweep integration coverage.
- Modify `docs/DEPLOYMENT.md`: document ProfeeX energy settings.

## Commit Policy

Tasks 1-3 are tightly coupled and must land as one green commit. Config, provider implementation, and factory routing all depend on each other; committing only one of those tasks can temporarily break existing ProfeeX bandwidth behavior or route ProfeeX energy to staking. Tasks 4 and 5 can be committed separately after their focused tests pass.

## Task 1: Config Model and Validation

**Files:**
- Modify: `app/profeex.py`
- Modify: `app/config.py`
- Test: `tests/test_resource_provider_config.py`

- [ ] **Step 1: Write failing config tests**

Add these tests to `tests/test_resource_provider_config.py`:

```python
    def test_profeex_required_for_profeex_energy_provider(self):
        with self.assertRaisesRegex(
            ValidationError,
            "PROFEEX must be configured when ENERGY_PROVIDER='profeex'",
        ):
            Settings(ENERGY_PROVIDER="profeex", BANDWIDTH_PROVIDER="disabled")

    def test_profeex_energy_provider_is_valid_when_configured(self):
        settings = Settings(
            ENERGY_PROVIDER="profeex",
            BANDWIDTH_PROVIDER="disabled",
            PROFEEX='{"api_key":"secret"}',
        )

        self.assertEqual(settings.ENERGY_PROVIDER, "profeex")
        self.assertEqual(settings.PROFEEX.fixed_energy_order_amount, 65_000)
        self.assertEqual(settings.PROFEEX.fixed_bandwidth_order_amount, 350)

    def test_profeex_config_rejects_energy_fixed_amount_below_provider_minimum(self):
        with self.assertRaises(ValidationError):
            ProfeeXConfig(api_key="secret", fixed_energy_order_amount=64_284)

    def test_profeex_config_rejects_energy_fixed_amount_above_provider_maximum(self):
        with self.assertRaises(ValidationError):
            ProfeeXConfig(api_key="secret", fixed_energy_order_amount=3_000_001)

    def test_profeex_config_rejects_bandwidth_fixed_amount_below_provider_minimum(self):
        with self.assertRaises(ValidationError):
            ProfeeXConfig(api_key="secret", fixed_bandwidth_order_amount=349)

    def test_profeex_config_rejects_bandwidth_fixed_amount_above_provider_maximum(self):
        with self.assertRaises(ValidationError):
            ProfeeXConfig(api_key="secret", fixed_bandwidth_order_amount=10_001)
```

Replace the existing `test_profeex_is_not_valid_energy_provider_yet` with `test_profeex_energy_provider_is_valid_when_configured`.

Replace the existing `test_profeex_config_rejects_bandwidth_min_below_api_minimum` and `test_profeex_config_rejects_bandwidth_max_above_api_maximum` methods with the fixed bandwidth tests shown above. The new public knobs are `fixed_bandwidth_order_amount` and `fixed_energy_order_amount`.

- [ ] **Step 2: Run config tests and verify failure**

Run:

```bash
uv run --python 3.12 --with-requirements requirements.txt python -m unittest tests.test_resource_provider_config -v
```

Expected: fail because `ENERGY_PROVIDER="profeex"` is not yet allowed and `ProfeeXConfig` does not yet expose fixed order fields.

- [ ] **Step 3: Implement ProfeeX config fields**

Edit `app/profeex.py` so the model has this shape:

```python
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, SecretStr, field_validator


PROFEEX_MIN_ENERGY_ORDER_AMOUNT = 64_285
PROFEEX_MAX_ENERGY_ORDER_AMOUNT = 3_000_000
PROFEEX_MIN_BANDWIDTH_ORDER_AMOUNT = 350
PROFEEX_MAX_BANDWIDTH_ORDER_AMOUNT = 10_000


class ProfeeXConfig(BaseModel):
    api_base_url: str = Field(default="https://api.profeex.io/api/v1", min_length=1)
    api_key: SecretStr
    currency: Literal["TRX", "USDT"] = "TRX"
    energy_duration_label: Literal["1h", "1d", "3d", "7d", "14d"] = "1h"
    bandwidth_duration_label: Literal["1h", "1d", "3d", "7d", "14d"] = "1h"
    fixed_energy_order_amount: int = Field(
        default=65_000,
        ge=PROFEEX_MIN_ENERGY_ORDER_AMOUNT,
        le=PROFEEX_MAX_ENERGY_ORDER_AMOUNT,
    )
    fixed_bandwidth_order_amount: int = Field(
        default=350,
        ge=PROFEEX_MIN_BANDWIDTH_ORDER_AMOUNT,
        le=PROFEEX_MAX_BANDWIDTH_ORDER_AMOUNT,
    )
    poll_interval_sec: float = Field(default=2.0, gt=0)
    timeout_sec: int = Field(default=60, gt=0)

    @field_validator("api_base_url")
    @classmethod
    def validate_api_base_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("api_base_url must be an HTTPS URL")
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("api_key must not be empty")
        return value

```

In `app/config.py`, change:

```python
ENERGY_PROVIDER: Literal["staking", "refee"] = "staking"
```

to:

```python
ENERGY_PROVIDER: Literal["staking", "refee", "profeex"] = "staking"
```

Add this validation after the re:Fee energy validation:

```python
        if self.ENERGY_PROVIDER == "profeex" and self.PROFEEX is None:
            raise ValueError(
                "PROFEEX must be configured when ENERGY_PROVIDER='profeex'"
            )
```

- [ ] **Step 4: Run config tests and verify pass**

Run:

```bash
uv run --python 3.12 --with-requirements requirements.txt python -m unittest tests.test_resource_provider_config -v
```

Expected: all tests in `tests.test_resource_provider_config` pass.

- [ ] **Step 5: Do not commit Task 1 yet**

Do not commit after Task 1. Continue directly to Task 2 because the current provider still reads the old bandwidth min/max config fields until Task 2 updates it.

Run this status check:

```bash
git status --short
```

Expected: only Task 1 files are modified, plus any pre-existing unrelated local changes.

## Task 2: ProfeeX Provider Energy Capability

**Files:**
- Modify: `app/resource_providers/profeex.py`
- Modify: `app/resource_providers/__init__.py`
- Modify: `app/energy_provider.py`
- Test: `tests/test_profeex_bandwidth_provider.py`

- [ ] **Step 1: Update test fakes**

In `tests/test_profeex_bandwidth_provider.py`, update `FakeSettings`:

```python
class FakeSettings:
    api_base_url = "https://api.profeex.test/api/v1"
    api_key = FakeSecret()
    currency = "TRX"
    energy_duration_label = "1h"
    bandwidth_duration_label = "1h"
    fixed_energy_order_amount = 65_000
    fixed_bandwidth_order_amount = 350
    poll_interval_sec = 0.01
    timeout_sec = 0.05
```

Add this energy resource fake below `SequencedBandwidthTronClient`:

```python
class SequencedEnergyTronClient:
    def __init__(self, resources):
        self.resources = list(resources)
        self.resource_calls = []

    def get_account_resource(self, address):
        self.resource_calls.append(address)
        if len(self.resources) > 1:
            return self.resources.pop(0)
        return self.resources[0]
```

- [ ] **Step 2: Write failing ProfeeX energy tests**

Add these tests to `ProfeeXBandwidthProviderTests`:

```python
    def test_rents_fixed_energy_with_query_params_and_api_key(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXProvider

        client = SequencedEnergyTronClient(
            [
                {"EnergyLimit": 0, "EnergyUsed": 0},
                {"EnergyLimit": 65_000, "EnergyUsed": 0},
            ]
        )
        provider = ProfeeXProvider(tron_client=client)
        posts = []
        gets = []

        def fake_post(url, params, headers, timeout):
            posts.append((url, params, headers, timeout))
            return MockJsonResponse(202, {"task_id": "task-1", "status": "QUEUED"})

        def fake_get(url, headers, timeout):
            gets.append((url, headers, timeout))
            return MockJsonResponse(200, {"task_id": "task-1", "status": "ACTIVE"})

        original_post = profeex.requests.post
        original_get = profeex.requests.get
        restore_config = self.patch_config(profeex)
        try:
            profeex.requests.post = fake_post
            profeex.requests.get = fake_get
            acquired = provider.acquire_energy(
                "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7",
                7_321,
                {"EnergyLimit": 0, "EnergyUsed": 0},
                minimum_energy_required=72_321,
            )
        finally:
            profeex.requests.post = original_post
            profeex.requests.get = original_get
            restore_config()

        self.assertTrue(acquired)
        self.assertEqual(
            posts,
            [
                (
                    "https://api.profeex.test/api/v1/delegation/buyenergy",
                    {
                        "target": "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7",
                        "volume": 65_000,
                        "days": "1h",
                        "currency": "TRX",
                    },
                    {"X-API-Key": "profeex-secret"},
                    10,
                )
            ],
        )
        self.assertEqual(
            gets,
            [
                (
                    "https://api.profeex.test/api/v1/delegation/status/task-1",
                    {"X-API-Key": "profeex-secret"},
                    gets[0][2],
                )
            ],
        )
        self.assertEqual(client.resource_calls, ["TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7", "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"])

    def test_skips_energy_order_when_fixed_threshold_is_already_available(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXProvider

        client = SequencedEnergyTronClient(
            [{"EnergyLimit": 64_500, "EnergyUsed": 0}]
        )
        provider = ProfeeXProvider(tron_client=client)
        original_post = profeex.requests.post
        restore_config = self.patch_config(profeex)
        try:
            profeex.requests.post = lambda *args, **kwargs: self.fail(
                "post not expected"
            )
            self.assertTrue(
                provider.acquire_energy(
                    "TADDR",
                    7_821,
                    {"EnergyLimit": 64_500, "EnergyUsed": 0},
                    minimum_energy_required=72_321,
                )
            )
        finally:
            profeex.requests.post = original_post
            restore_config()

    def test_active_status_requires_post_delegation_energy_recheck(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXProvider

        client = SequencedEnergyTronClient(
            [
                {"EnergyLimit": 0, "EnergyUsed": 0},
                {"EnergyLimit": 64_499, "EnergyUsed": 0},
            ]
        )
        provider = ProfeeXProvider(tron_client=client)

        original_post = profeex.requests.post
        original_get = profeex.requests.get
        restore_config = self.patch_config(profeex)
        try:
            profeex.requests.post = lambda *args, **kwargs: MockJsonResponse(
                202, {"task_id": "task-1", "status": "QUEUED"}
            )
            profeex.requests.get = lambda *args, **kwargs: MockJsonResponse(
                200, {"task_id": "task-1", "status": "ACTIVE"}
            )
            self.assertFalse(
                provider.acquire_energy(
                    "TADDR",
                    7_321,
                    {"EnergyLimit": 0, "EnergyUsed": 0},
                    minimum_energy_required=72_321,
                )
            )
        finally:
            profeex.requests.post = original_post
            profeex.requests.get = original_get
            restore_config()

        self.assertEqual(client.resource_calls, ["TADDR", "TADDR"])

    def test_release_energy_is_noop(self):
        from app.resource_providers.profeex import ProfeeXProvider

        provider = ProfeeXProvider()
        self.assertIsNone(provider.release_energy("TADDR"))

    def test_bandwidth_uses_fixed_order_amount_not_required_amount(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXProvider

        client = SequencedBandwidthTronClient(
            [
                {"freeNetLimit": 600, "freeNetUsed": 600, "NetLimit": 0, "NetUsed": 0},
                {"freeNetLimit": 600, "freeNetUsed": 600, "NetLimit": 350, "NetUsed": 0},
            ]
        )
        provider = ProfeeXProvider(tron_client=client)
        posts = []

        def fake_post(url, params, headers, timeout):
            posts.append((url, params, headers, timeout))
            return MockJsonResponse(202, {"task_id": "task-1", "status": "ACTIVE"})

        original_post = profeex.requests.post
        restore_config = self.patch_config(profeex)
        try:
            profeex.requests.post = fake_post
            self.assertTrue(provider.acquire_bandwidth("TADDR", 346))
        finally:
            profeex.requests.post = original_post
            restore_config()

        self.assertEqual(posts[0][1]["volume"], 350)
```

Update existing expected bandwidth post payloads to use `fixed_bandwidth_order_amount`. Keep the expected value `350`.

Update direct `_wait_until_active()` helper tests to pass the new `resource_name` argument:

```python
provider._wait_until_active(FakeSettings(), "task-1", order, "bandwidth")
```

For `test_pending_status_polls_before_sleeping`, update the call to:

```python
provider._wait_until_active(
    FakeSettings(),
    "task-1",
    {"task_id": "task-1", "status": "QUEUED"},
    "bandwidth",
)
```

Rename the existing `test_fails_when_requested_bandwidth_exceeds_provider_maximum` to:

```python
    def test_fixed_bandwidth_below_large_required_fails_before_order(self):
```

Keep the same basic assertion pattern, but assert the new reason: a fixed `350` bandwidth order cannot satisfy a `10_001` required bandwidth request, so the provider must fail before making an API call.

- [ ] **Step 3: Write failing insufficient fixed bandwidth test**

Add this test:

```python
    def test_fixed_bandwidth_below_required_fails_before_order(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXProvider

        class LowFixedBandwidthSettings(FakeSettings):
            fixed_bandwidth_order_amount = 350

        client = SequencedBandwidthTronClient(
            [{"freeNetLimit": 0, "freeNetUsed": 0, "NetLimit": 0, "NetUsed": 0}]
        )
        provider = ProfeeXProvider(tron_client=client)
        original_post = profeex.requests.post
        original_config = profeex.config
        try:
            profeex.config = SimpleNamespace(PROFEEX=LowFixedBandwidthSettings())
            profeex.requests.post = lambda *args, **kwargs: self.fail(
                "post not expected"
            )
            self.assertFalse(provider.acquire_bandwidth("TADDR", 351))
        finally:
            profeex.requests.post = original_post
            profeex.config = original_config
```

- [ ] **Step 4: Run ProfeeX provider tests and verify failure**

Run:

```bash
uv run --python 3.12 --with-requirements requirements.txt python -m unittest tests.test_profeex_bandwidth_provider -v
```

Expected: fail because `ProfeeXProvider`, `acquire_energy()`, fixed bandwidth behavior, and `release_energy()` are not implemented yet.

- [ ] **Step 5: Implement generalized ProfeeX provider**

In `app/resource_providers/profeex.py`, add imports:

```python
from .base import BandwidthProvider, EnergyProvider
from ..utils import get_available_energy, has_free_bw
```

Rename the implementation class and keep the alias:

```python
class ProfeeXProvider(EnergyProvider, BandwidthProvider):
    REQUEST_TIMEOUT_SEC = 10
    PENDING_STATUSES = {"QUEUED", "PENDING", "PROCESSING"}
    SUCCESS_STATUSES = {"ACTIVE"}
    FAILURE_STATUSES = {"FAILED", "CANCELLED", "COMPLETED", "unknown"}
    FIXED_ENERGY_ORDER_TOLERANCE = 500

    def __init__(self, tron_client=None):
        self.tron_client = tron_client
```

Add `acquire_energy()`:

```python
    def acquire_energy(
        self,
        receiver: str,
        energy_to_provision: int,
        account_resource: dict,
        *,
        minimum_energy_required: int | None = None,
    ) -> bool:
        settings = config.PROFEEX
        if settings is None:
            logger.warning("PROFEEX config is missing. Terminating transfer.")
            return False

        threshold = max(
            settings.fixed_energy_order_amount - self.FIXED_ENERGY_ORDER_TOLERANCE,
            0,
        )
        tron_client = self.tron_client or ConnectionManager.client()
        onetime_energy_available = self._get_available_energy(
            tron_client, receiver, "pre-order"
        )
        if onetime_energy_available is None:
            return False
        if onetime_energy_available >= threshold:
            logger.info(
                f"ProfeeX energy order not needed for {receiver}: "
                f"{onetime_energy_available=} energy_threshold={threshold}"
            )
            return True

        amount = settings.fixed_energy_order_amount
        logger.info(
            f"Requesting ProfeeX energy rental for {receiver}: "
            f"{amount} energy for {settings.energy_duration_label}"
        )

        order = self._create_order(
            settings,
            receiver,
            amount,
            resource_name="energy",
            path="/delegation/buyenergy",
            duration_label=settings.energy_duration_label,
        )
        if order is None:
            return False

        task_id = self._extract_task_id(order, "energy")
        if task_id is None:
            return False

        active_order = self._wait_until_active(settings, task_id, order, "energy")
        if active_order is None:
            return False

        onetime_energy_available = self._get_available_energy(
            tron_client, receiver, "post-delegation"
        )
        if onetime_energy_available is None:
            return False
        if onetime_energy_available < threshold:
            logger.warning(
                "Onetime account has not enough energy after ProfeeX delegation. "
                "Terminating transfer."
            )
            return False

        logger.info(f"ProfeeX energy successfully delegated: {active_order}")
        return True
```

Replace `acquire_bandwidth()` sizing with fixed sizing:

```python
        amount = settings.fixed_bandwidth_order_amount
        if amount < bandwidth_required:
            logger.warning(
                "ProfeeX fixed bandwidth order amount is below required bandwidth: "
                f"{amount=} {bandwidth_required=}"
            )
            return False
```

Then call the shared order helper:

```python
        order = self._create_order(
            settings,
            receiver,
            amount,
            resource_name="bandwidth",
            path="/delegation/buybandwidth",
            duration_label=settings.bandwidth_duration_label,
        )
```

Add shared helpers:

```python
    def release_energy(self, receiver: str) -> None:
        logger.info(
            f"ProfeeX energy for {receiver} returns after rent expiration. "
            "Skipping undelegate."
        )

    def _create_order(
        self,
        settings,
        receiver: str,
        amount: int,
        *,
        resource_name: str,
        path: str,
        duration_label: str,
    ) -> dict | None:
        try:
            response = requests.post(
                self._url(settings, path),
                params={
                    "target": receiver,
                    "volume": amount,
                    "days": duration_label,
                    "currency": settings.currency,
                },
                headers=self._headers(settings),
                timeout=self.REQUEST_TIMEOUT_SEC,
            )
        except requests.RequestException:
            logger.exception(f"ProfeeX create {resource_name} order request failed")
            return None

        if response.status_code != 202:
            logger.warning(
                f"ProfeeX create {resource_name} order rejected with status "
                f"{response.status_code}: {response.text}"
            )
            return None

        try:
            data = response.json()
        except ValueError:
            logger.exception(
                f"ProfeeX create {resource_name} order response is not valid JSON"
            )
            return None
        if not isinstance(data, dict):
            logger.warning(
                f"ProfeeX create {resource_name} order response is not an object: {data}"
            )
            return None

        logger.info(f"ProfeeX {resource_name} order accepted: {data}")
        return data

    def _extract_task_id(self, order: dict, resource_name: str) -> str | None:
        task_id = order.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            logger.warning(f"ProfeeX {resource_name} order response has no task_id: {order}")
            return None
        if "status" not in order:
            order["status"] = "PENDING"
        return task_id

    def _get_available_energy(self, tron_client, receiver: str, stage: str) -> int | None:
        try:
            return get_available_energy(tron_client.get_account_resource(receiver))
        except Exception:
            logger.exception(
                f"Unable to read ProfeeX receiver energy during {stage}: {receiver}"
            )
            return None
```

Update `_wait_until_active()` to accept `resource_name` and use it in log messages:

```python
    def _wait_until_active(
        self, settings, task_id: str, initial_order: dict, resource_name: str
    ) -> dict | None:
```

At the bottom of `app/resource_providers/profeex.py`, keep:

```python
ProfeeXBandwidthProvider = ProfeeXProvider
```

- [ ] **Step 6: Update exports**

In `app/resource_providers/__init__.py`, import and export both names:

```python
from .profeex import ProfeeXBandwidthProvider, ProfeeXProvider
```

Add `"ProfeeXProvider"` to `__all__`.

In `app/energy_provider.py`, import and export `ProfeeXProvider`:

```python
from .resource_providers import (
    BandwidthProvider,
    EnergyProvider,
    ProfeeXBandwidthProvider,
    ProfeeXProvider,
    RefeeProvider,
    StakingEnergyProvider,
)
```

Add `"ProfeeXProvider"` to `__all__`.

- [ ] **Step 7: Run ProfeeX provider tests and verify pass**

Run:

```bash
uv run --python 3.12 --with-requirements requirements.txt python -m unittest tests.test_profeex_bandwidth_provider -v
```

Expected: all tests in `tests.test_profeex_bandwidth_provider` pass.

- [ ] **Step 8: Do not commit Task 2 yet**

Do not commit after Task 2. Continue directly to Task 3 so config, provider implementation, and factory routing land together.

Run this status check:

```bash
git status --short
```

Expected: Task 1 and Task 2 files are modified, plus any pre-existing unrelated local changes.

## Task 3: Provider Factory Wiring

**Files:**
- Modify: `app/resource_providers/factory.py`
- Test: `tests/test_resource_provider_factory.py`

- [ ] **Step 1: Write failing factory test**

Add this test to `tests/test_resource_provider_factory.py`:

```python
    def test_energy_factory_returns_profeex_provider(self):
        from app.resource_providers import factory
        from app.resource_providers.profeex import ProfeeXProvider

        original_config = factory.config
        try:
            factory.config = SimpleNamespace(ENERGY_PROVIDER="profeex")
            provider = factory.get_energy_provider(tron_client=object())
        finally:
            factory.config = original_config

        self.assertIsInstance(provider, ProfeeXProvider)
```

- [ ] **Step 2: Run factory tests and verify failure**

Run:

```bash
uv run --python 3.12 --with-requirements requirements.txt python -m unittest tests.test_resource_provider_factory -v
```

Expected: fail because `get_energy_provider()` still falls through to `StakingEnergyProvider` for ProfeeX.

- [ ] **Step 3: Implement factory branch**

Update `app/resource_providers/factory.py` imports:

```python
from .profeex import ProfeeXBandwidthProvider, ProfeeXProvider
```

Update `get_energy_provider()`:

```python
def get_energy_provider(tron_client=None) -> EnergyProvider:
    if config.ENERGY_PROVIDER == "refee":
        return RefeeProvider(tron_client=tron_client)
    if config.ENERGY_PROVIDER == "profeex":
        return ProfeeXProvider(tron_client=tron_client)
    return StakingEnergyProvider(tron_client=tron_client)
```

- [ ] **Step 4: Run focused provider/config/factory tests and verify pass**

Run:

```bash
uv run --python 3.12 --with-requirements requirements.txt python -m unittest tests.test_resource_provider_config tests.test_resource_provider_factory tests.test_profeex_bandwidth_provider -v
```

Expected: all config, factory, and ProfeeX provider tests pass.

- [ ] **Step 5: Commit Tasks 1-3 as one green slice**

Run:

```bash
git add app/profeex.py app/config.py app/resource_providers/profeex.py app/resource_providers/__init__.py app/energy_provider.py app/resource_providers/factory.py tests/test_resource_provider_config.py tests/test_profeex_bandwidth_provider.py tests/test_resource_provider_factory.py
git commit -m "feat: add profeex energy provider"
```

## Task 4: Sweep Task Integration

**Files:**
- Modify: `app/tasks.py`
- Test: `tests/test_refee_bandwidth_guard.py`

- [ ] **Step 1: Add ProfeeX config and provider fakes**

Add these helpers to `tests/test_refee_bandwidth_guard.py`:

```python
class ProfeeXEnergyConfig(FakeConfig):
    ENERGY_PROVIDER = "profeex"
    BANDWIDTH_PROVIDER = "profeex"
    ENERGY_DELEGATION_MODE = False
    ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT = True
    TX_FEE_LIMIT = Decimal("50")


class OrderedEnergyProvider(FakeProvider):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def acquire_energy(self, *_args, **_kwargs):
        self.events.append("energy")
        self.acquire_calls += 1
        return False


class OrderedBandwidthProvider(FakeProvider):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def acquire_bandwidth(self, receiver, bandwidth_required):
        self.events.append("bandwidth")
        return super().acquire_bandwidth(receiver, bandwidth_required)


class SuccessfulFakeProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.release_calls = []

    def acquire_energy(self, *_args, **_kwargs):
        self.acquire_calls += 1
        return True

    def release_energy(self, receiver):
        self.release_calls.append(receiver)


class FakeTokenTransfer:
    txid = "token-txid"

    def __init__(self):
        self._raw_data = {}

    def with_owner(self, _owner):
        return self

    def fee_limit(self, _fee_limit):
        return self

    def build(self):
        return self

    def sign(self, _private_key):
        return self

    def broadcast(self):
        return self

    def wait(self):
        return {"receipt": {"result": "SUCCESS"}}


class SuccessfulContractFunctions(FakeContractFunctions):
    def transfer(self, _address, _amount):
        return FakeTokenTransfer()


class SuccessfulContract:
    functions = SuccessfulContractFunctions()


class SuccessfulSweepTronClient(FakeTronClient):
    def get_contract(self, _contract_address):
        return SuccessfulContract()

    def get_account_resource(self, _address):
        return {
            "EnergyLimit": 0,
            "freeNetLimit": 600,
            "freeNetUsed": 600,
            "NetLimit": 350,
            "NetUsed": 0,
        }
```

- [ ] **Step 2: Write failing task-level ProfeeX tests**

Add these tests to `RefeeBandwidthGuardTests`:

```python
    def test_profeex_energy_provider_enters_provider_mode_and_rents_bandwidth_first(self):
        from app import tasks
        from app.schemas import KeyType

        fee_deposit = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
        onetime = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
        client = FakeTronClient()
        events = []
        energy_provider = OrderedEnergyProvider(events)
        bandwidth_provider = OrderedBandwidthProvider(events)
        original_config = tasks.config
        original_connection_manager = tasks.ConnectionManager
        original_get_key = tasks.get_key
        original_get_energy_provider = tasks.get_energy_provider
        original_get_bandwidth_provider = tasks.get_bandwidth_provider
        try:
            tasks.config = ProfeeXEnergyConfig()
            tasks.ConnectionManager = SimpleNamespace(client=lambda: client)

            def fake_get_key(key_type, pub=None):
                if key_type == KeyType.fee_deposit:
                    return object(), fee_deposit
                if key_type == KeyType.onetime:
                    return object(), pub
                raise AssertionError(f"unexpected key type {key_type}")

            tasks.get_key = fake_get_key
            tasks.get_energy_provider = lambda tron_client=None: energy_provider
            tasks.get_bandwidth_provider = lambda tron_client=None: bandwidth_provider

            result = tasks.transfer_trc20_from.run(onetime, "USDT")
        finally:
            tasks.config = original_config
            tasks.ConnectionManager = original_connection_manager
            tasks.get_key = original_get_key
            tasks.get_energy_provider = original_get_energy_provider
            tasks.get_bandwidth_provider = original_get_bandwidth_provider

        self.assertIsNone(result)
        self.assertEqual(
            bandwidth_provider.acquire_bandwidth_calls,
            [(onetime, ProfeeXEnergyConfig.BANDWIDTH_PER_TRC20_TRANSFER_CALL)],
        )
        self.assertEqual(energy_provider.acquire_calls, 1)
        self.assertEqual(client.energy_estimate_calls, 1)
        self.assertEqual(events, ["bandwidth", "energy"])

    def test_profeex_energy_failure_does_not_use_refee_burn_fallback(self):
        from app import tasks
        from app.schemas import KeyType

        fee_deposit = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
        onetime = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
        client = FakeTronClient()
        energy_provider = FakeProvider()
        bandwidth_provider = FakeProvider()
        original_config = tasks.config
        original_connection_manager = tasks.ConnectionManager
        original_get_key = tasks.get_key
        original_get_energy_provider = tasks.get_energy_provider
        original_get_bandwidth_provider = tasks.get_bandwidth_provider
        original_fund_onetime = tasks._fund_onetime_for_trc20_burn
        try:
            tasks.config = ProfeeXEnergyConfig()
            tasks.ConnectionManager = SimpleNamespace(client=lambda: client)

            def fake_get_key(key_type, pub=None):
                if key_type == KeyType.fee_deposit:
                    return object(), fee_deposit
                if key_type == KeyType.onetime:
                    return object(), pub
                raise AssertionError(f"unexpected key type {key_type}")

            tasks.get_key = fake_get_key
            tasks.get_energy_provider = lambda tron_client=None: energy_provider
            tasks.get_bandwidth_provider = lambda tron_client=None: bandwidth_provider
            tasks._fund_onetime_for_trc20_burn = lambda *args, **kwargs: self.fail(
                "ProfeeX provider failure must not use re:Fee burn fallback"
            )

            result = tasks.transfer_trc20_from.run(onetime, "USDT")
        finally:
            tasks.config = original_config
            tasks.ConnectionManager = original_connection_manager
            tasks.get_key = original_get_key
            tasks.get_energy_provider = original_get_energy_provider
            tasks.get_bandwidth_provider = original_get_bandwidth_provider
            tasks._fund_onetime_for_trc20_burn = original_fund_onetime

        self.assertIsNone(result)
        self.assertEqual(energy_provider.acquire_calls, 1)

    def test_profeex_successful_sweep_calls_release_energy(self):
        from app import tasks
        from app.schemas import KeyType

        fee_deposit = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
        onetime = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
        client = SuccessfulSweepTronClient()
        provider = SuccessfulFakeProvider()
        original_config = tasks.config
        original_connection_manager = tasks.ConnectionManager
        original_get_key = tasks.get_key
        original_get_energy_provider = tasks.get_energy_provider
        original_get_bandwidth_provider = tasks.get_bandwidth_provider
        try:
            tasks.config = ProfeeXEnergyConfig()
            tasks.ConnectionManager = SimpleNamespace(client=lambda: client)

            def fake_get_key(key_type, pub=None):
                if key_type == KeyType.fee_deposit:
                    return object(), fee_deposit
                if key_type == KeyType.onetime:
                    return object(), pub
                raise AssertionError(f"unexpected key type {key_type}")

            tasks.get_key = fake_get_key
            tasks.get_energy_provider = lambda tron_client=None: provider
            tasks.get_bandwidth_provider = lambda tron_client=None: provider

            result = tasks.transfer_trc20_from.run(onetime, "USDT")
        finally:
            tasks.config = original_config
            tasks.ConnectionManager = original_connection_manager
            tasks.get_key = original_get_key
            tasks.get_energy_provider = original_get_energy_provider
            tasks.get_bandwidth_provider = original_get_bandwidth_provider

        self.assertEqual(result["tx_token"], {"receipt": {"result": "SUCCESS"}})
        self.assertEqual(provider.release_calls, [onetime])
```

- [ ] **Step 3: Run task integration tests and verify failure**

Run:

```bash
uv run --python 3.12 --with-requirements requirements.txt python -m unittest tests.test_refee_bandwidth_guard -v
```

Expected: ProfeeX task tests fail because `tasks.py` does not yet treat `ENERGY_PROVIDER="profeex"` as provider mode.

- [ ] **Step 4: Implement task control flow**

In `app/tasks.py`, replace:

```python
    use_refee_energy_provider = config.ENERGY_PROVIDER == "refee"
    use_staking_energy_provider = (
        config.ENERGY_PROVIDER == "staking" and config.ENERGY_DELEGATION_MODE
    )
    use_energy_provider = use_refee_energy_provider or use_staking_energy_provider
```

with:

```python
    use_refee_energy_provider = config.ENERGY_PROVIDER == "refee"
    use_external_energy_provider = config.ENERGY_PROVIDER in {"refee", "profeex"}
    use_staking_energy_provider = (
        config.ENERGY_PROVIDER == "staking" and config.ENERGY_DELEGATION_MODE
    )
    use_energy_provider = use_external_energy_provider or use_staking_energy_provider
```

Replace:

```python
            if use_refee_energy_provider:
                energy_to_provision = energy_needed - onetime_energy_available
            else:
```

with:

```python
            if use_external_energy_provider:
                energy_to_provision = energy_needed - onetime_energy_available
            else:
```

Do not change the fallback guard:

```python
                    if (
                        use_refee_energy_provider
                        and config.ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT
                    ):
```

That guard must remain re:Fee-only.

- [ ] **Step 5: Run task integration tests and verify pass**

Run:

```bash
uv run --python 3.12 --with-requirements requirements.txt python -m unittest tests.test_refee_bandwidth_guard -v
```

Expected: all task integration tests pass.

- [ ] **Step 6: Commit Task 4**

Run:

```bash
git add app/tasks.py tests/test_refee_bandwidth_guard.py
git commit -m "feat: use profeex in sweep energy flow"
```

## Task 5: Deployment Documentation and Full Verification

**Files:**
- Modify: `docs/DEPLOYMENT.md`

- [ ] **Step 1: Update deployment docs**

In `docs/DEPLOYMENT.md`, update the ProfeeX resource provider example to include:

```yaml
ENERGY_PROVIDER: "profeex"
BANDWIDTH_PROVIDER: "profeex"
PROFEEX: '{"api_key":"REPLACE_WITH_PROFEEX_API_KEY","energy_duration_label":"1h","bandwidth_duration_label":"1h","currency":"TRX","fixed_energy_order_amount":65000,"fixed_bandwidth_order_amount":350}'
```

Add this explanation near the example:

```markdown
`fixed_energy_order_amount` and `fixed_bandwidth_order_amount` are the actual order sizes sent to ProfeeX. They are not API min/max fields. With the default `fixed_energy_order_amount=65000`, the app treats `64500` available energy as sufficient to avoid duplicate fixed rentals.
```

- [ ] **Step 2: Run focused test suite**

Run:

```bash
uv run --python 3.12 --with-requirements requirements.txt python -m unittest tests.test_resource_provider_config tests.test_resource_provider_factory tests.test_profeex_bandwidth_provider tests.test_refee_bandwidth_guard -v
```

Expected: all focused tests pass.

- [ ] **Step 3: Run full test suite**

Run:

```bash
uv run --python 3.12 --with-requirements requirements.txt python -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 4: Run whitespace check**

Run before committing the docs change:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 5: Commit Task 5**

Run:

```bash
git add docs/DEPLOYMENT.md
git commit -m "docs: document profeex energy settings"
```

- [ ] **Step 6: Run committed-range whitespace check**

Run after the Task 5 commit:

```bash
git diff --check 1691ead..HEAD
```

Expected: no output.

## Final Review Checklist

- [ ] `ENERGY_PROVIDER="profeex"` is accepted only with `PROFEEX`.
- [ ] ProfeeX energy order uses `/delegation/buyenergy`.
- [ ] ProfeeX energy order volume is `fixed_energy_order_amount`, default `65000`.
- [ ] ProfeeX skips duplicate energy rental when available energy is at least `fixed_energy_order_amount - 500`.
- [ ] ProfeeX bandwidth order volume is `fixed_bandwidth_order_amount`, default `350`.
- [ ] ProfeeX fails before ordering bandwidth if fixed amount is below required bandwidth.
- [ ] `release_energy()` is safe for ProfeeX.
- [ ] ProfeeX provider failure does not use re:Fee burn fallback.
- [ ] Existing re:Fee and staking tests pass.
- [ ] Deployment docs include the exact new JSON config shape.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-21-profeex-energy-provider.md`. Two execution options:

1. **Subagent-Driven (recommended)** - dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** - execute tasks in this session using executing-plans, batch execution with checkpoints.

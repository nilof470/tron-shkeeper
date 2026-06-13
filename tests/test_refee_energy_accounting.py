from decimal import Decimal
from types import SimpleNamespace
import unittest


FEE_DEPOSIT = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
ONETIME = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"


class FakeConfig:
    ENERGY_PROVIDER = "refee"
    BANDWIDTH_PROVIDER = "disabled"
    ENERGY_DELEGATION_MODE = False
    ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT = False
    ENERGY_DELEGATION_MODE_ALLOW_ADDITIONAL_ENERGY_DELEGATION = False
    BANDWIDTH_PER_TRC20_TRANSFER_CALL = 346
    REFEE_FIXED_ENERGY_ORDER_AMOUNT = 65_000
    TX_FEE_LIMIT = Decimal("50")

    def get_contract_address(self, _symbol):
        return "TCONTRACT"

    def get_min_transfer_threshold(self, _symbol):
        return Decimal("1")


class FakeStakingConfig(FakeConfig):
    ENERGY_PROVIDER = "staking"
    ENERGY_DELEGATION_MODE = True
    BANDWIDTH_PER_DELEGE_CALL = 1
    BANDWIDTH_PER_UNDELEGATE_CALL = 1
    BANDWIDTH_PER_TRX_TRANSFER = 1
    ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH = False


class FakeContractFunctions:
    def decimals(self):
        return 6

    def balanceOf(self, _address):
        return 3_000_000

    def transfer(self, _dst, _amount):
        return FakeTx()


class FakeContract:
    functions = FakeContractFunctions()


class FailedTransferContractFunctions(FakeContractFunctions):
    def transfer(self, _dst, _amount):
        return FailedTransferTx()


class FailedTransferContract:
    functions = FailedTransferContractFunctions()


class NoBroadcastContractFunctions(FakeContractFunctions):
    def transfer(self, _dst, _amount):
        raise AssertionError("token transfer must not be built before resources are ready")


class NoBroadcastContract:
    functions = NoBroadcastContractFunctions()


class FakeTx:
    txid = "txid"
    _raw_data = {}

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


class FailedTransferTx(FakeTx):
    txid = "failed-token-txid"

    def wait(self):
        return {
            "receipt": {"result": "OUT_OF_ENERGY"},
            "result": "FAILED",
            "resMessage": "out of energy",
        }


class FakeTronClient:
    def __init__(self, account_resource, delegated_resource_index=None):
        self.account_resource = account_resource
        self.delegated_resource_index = delegated_resource_index or {}

    def get_contract(self, _contract_address):
        return FakeContract()

    def get_account_resource(self, _address):
        return self.account_resource

    def get_estimated_energy(self, *_args, **_kwargs):
        return 50_000

    def get_delegated_resource_account_index_v2(self, _address):
        return self.delegated_resource_index


class SequencedResourceTronClient(FakeTronClient):
    def __init__(self, account_resources):
        self.account_resources = list(account_resources)
        super().__init__(self.account_resources[0])

    def get_account_resource(self, _address):
        if len(self.account_resources) > 1:
            return self.account_resources.pop(0)
        return self.account_resources[0]


class FailedTransferTronClient(FakeTronClient):
    def get_contract(self, _contract_address):
        return FailedTransferContract()


class NoBroadcastTronClient(FakeTronClient):
    def get_contract(self, _contract_address):
        return NoBroadcastContract()


class RecordingProvider:
    def __init__(self, acquire_result=False):
        self.acquire_result = acquire_result
        self.acquire_calls = []
        self.release_calls = []

    def acquire_energy(self, *args, **kwargs):
        self.acquire_calls.append((args, kwargs))
        return self.acquire_result

    def release_energy(self, receiver):
        self.release_calls.append(receiver)


class RefeeEnergyAccountingTests(unittest.TestCase):
    def patch_tasks(
        self,
        tasks,
        client,
        provider,
        config=None,
        ensure_usdt_resources=None,
        fail_legacy_providers=False,
    ):
        missing = object()
        original_config = tasks.config
        original_connection_manager = tasks.ConnectionManager
        original_get_key = tasks.get_key
        original_get_energy_delegator = tasks.get_energy_delegator
        original_get_energy_provider = tasks.get_energy_provider
        original_get_bandwidth_provider = tasks.get_bandwidth_provider
        original_is_sweep_allowed = tasks.is_sweep_allowed
        original_ensure_usdt_resources = getattr(
            tasks,
            "ensure_usdt_transfer_resources",
            missing,
        )

        tasks.config = config or FakeConfig()
        tasks.ConnectionManager = SimpleNamespace(client=lambda: client)

        def fake_get_key(key_type, pub=None):
            from app.schemas import KeyType

            if key_type == KeyType.fee_deposit:
                return object(), FEE_DEPOSIT
            if key_type == KeyType.onetime:
                return object(), pub
            raise AssertionError(f"unexpected key type {key_type}")

        tasks.get_key = fake_get_key
        tasks.get_energy_delegator = lambda: (object(), "TDELEGATOR")
        if fail_legacy_providers:
            tasks.get_energy_provider = lambda tron_client=None: (_ for _ in ()).throw(
                AssertionError("external USDT sweep must not call legacy energy provider")
            )
            tasks.get_bandwidth_provider = lambda tron_client=None: (
                _ for _ in ()
            ).throw(
                AssertionError(
                    "external USDT sweep must not call legacy bandwidth provider"
                )
            )
        else:
            tasks.get_energy_provider = lambda tron_client=None: provider
            tasks.get_bandwidth_provider = lambda tron_client=None: None
        if ensure_usdt_resources is not None:
            tasks.ensure_usdt_transfer_resources = ensure_usdt_resources
        tasks.is_sweep_allowed = lambda *_args, **_kwargs: True

        def restore():
            tasks.config = original_config
            tasks.ConnectionManager = original_connection_manager
            tasks.get_key = original_get_key
            tasks.get_energy_delegator = original_get_energy_delegator
            tasks.get_energy_provider = original_get_energy_provider
            tasks.get_bandwidth_provider = original_get_bandwidth_provider
            tasks.is_sweep_allowed = original_is_sweep_allowed
            if original_ensure_usdt_resources is missing:
                if hasattr(tasks, "ensure_usdt_transfer_resources"):
                    delattr(tasks, "ensure_usdt_transfer_resources")
            else:
                tasks.ensure_usdt_transfer_resources = original_ensure_usdt_resources

        return restore

    def test_refee_usdt_sweep_uses_shared_resources_instead_of_legacy_energy_accounting(self):
        from app import tasks

        provider = RecordingProvider(acquire_result=False)
        calls = []
        client = FakeTronClient(
            {
                "EnergyLimit": 100_000,
                "EnergyUsed": 90_000,
                "freeNetLimit": 600,
                "freeNetUsed": 0,
                "NetLimit": 0,
                "NetUsed": 0,
            }
        )

        def ensure(source, destination, amount, *, tron_client=None):
            calls.append((source, destination, amount, tron_client))

        restore = self.patch_tasks(
            tasks,
            client,
            provider,
            ensure_usdt_resources=ensure,
            fail_legacy_providers=True,
        )
        try:
            result = tasks.transfer_trc20_from.run(ONETIME, "USDT")
        finally:
            restore()

        self.assertEqual(result["tx_token"], {"receipt": {"result": "SUCCESS"}})
        self.assertEqual(calls, [(ONETIME, FEE_DEPOSIT, Decimal("3"), client)])
        self.assertEqual(provider.acquire_calls, [])

    def test_staking_acquires_missing_energy_when_no_delegated_accounts_exist(self):
        from app import tasks

        provider = RecordingProvider(acquire_result=False)
        client = FakeTronClient(
            {
                "EnergyLimit": 100_000,
                "EnergyUsed": 90_000,
                "freeNetLimit": 600,
                "freeNetUsed": 0,
                "NetLimit": 0,
                "NetUsed": 0,
            }
        )
        restore = self.patch_tasks(tasks, client, provider, FakeStakingConfig())
        try:
            result = tasks.transfer_trc20_from.run(ONETIME, "USDT")
        finally:
            restore()

        self.assertIsNone(result)
        self.assertEqual(len(provider.acquire_calls), 1)
        args, kwargs = provider.acquire_calls[0]
        self.assertEqual(args[0], ONETIME)
        self.assertEqual(args[1], 40_000)
        self.assertEqual(kwargs["minimum_energy_required"], 50_000)

    def test_refee_usdt_sweep_resource_error_skips_legacy_energy_accounting(self):
        from app import tasks
        from app.usdt_resource_provisioning import UsdtResourceError

        provider = RecordingProvider(acquire_result=False)
        client = NoBroadcastTronClient(
            {
                "EnergyLimit": 0,
                "EnergyUsed": 0,
                "freeNetLimit": 600,
                "freeNetUsed": 0,
                "NetLimit": 999,
                "NetUsed": 0,
            },
        )

        def ensure(*_args, **_kwargs):
            raise UsdtResourceError(
                "resources unavailable",
                code="PROVIDER_FAILED",
                temporary=True,
            )

        restore = self.patch_tasks(
            tasks,
            client,
            provider,
            ensure_usdt_resources=ensure,
            fail_legacy_providers=True,
        )
        try:
            result = tasks.transfer_trc20_from.run(ONETIME, "USDT")
        finally:
            restore()

        self.assertIsNone(result)
        self.assertEqual(provider.acquire_calls, [])

    def test_failed_trc20_receipt_is_not_treated_as_successful_sweep(self):
        from app import tasks

        provider = RecordingProvider(acquire_result=True)
        calls = []
        client = FailedTransferTronClient(
            {
                "EnergyLimit": 0,
                "EnergyUsed": 0,
                "freeNetLimit": 600,
                "freeNetUsed": 0,
                "NetLimit": 0,
                "NetUsed": 0,
            }
        )

        def ensure(source, destination, amount, *, tron_client=None):
            calls.append((source, destination, amount, tron_client))

        restore = self.patch_tasks(
            tasks,
            client,
            provider,
            ensure_usdt_resources=ensure,
            fail_legacy_providers=True,
        )
        try:
            result = tasks.transfer_trc20_from.run(ONETIME, "USDT")
        finally:
            restore()

        self.assertIsNone(result)
        self.assertEqual(calls, [(ONETIME, FEE_DEPOSIT, Decimal("3"), client)])
        self.assertEqual(provider.release_calls, [])

    def test_refee_provider_orders_delta_but_verifies_total_available_energy(self):
        from app.energy_provider import RefeeEnergyProvider

        class FakeSettings:
            energy_overprovision_factor = Decimal("1.05")
            min_energy_order_amount = 30_000
            rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01

        provider = RefeeEnergyProvider(
            tron_client=SequencedResourceTronClient(
                [
                    {
                        "EnergyLimit": 100_000,
                        "EnergyUsed": 50_000,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                    {
                        "EnergyLimit": 100_000,
                        "EnergyUsed": 20_000,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                ]
            )
        )
        created_orders = []
        provider._create_order = lambda settings, receiver, amount: created_orders.append(
            (receiver, amount)
        ) or {"id": "order-1", "status": "pending"}
        provider._wait_until_delegated = lambda settings, order_id, order: {
            "id": order_id,
            "status": "delegated",
        }

        original_config = __import__("app.resource_providers.refee", fromlist=["config"]).config
        __import__("app.resource_providers.refee", fromlist=["config"]).config = SimpleNamespace(
            REFEE=FakeSettings()
        )
        try:
            acquired = provider.acquire_energy(
                ONETIME,
                30_000,
                {},
                minimum_energy_required=80_000,
            )
        finally:
            __import__("app.resource_providers.refee", fromlist=["config"]).config = original_config

        self.assertTrue(acquired)
        self.assertEqual(created_orders, [(ONETIME, 31_500)])

    def test_refee_provider_uses_fixed_order_amount_when_configured(self):
        from app.energy_provider import RefeeEnergyProvider

        class FakeSettings:
            energy_overprovision_factor = Decimal("1.01")
            min_energy_order_amount = 30_000
            rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01

        provider = RefeeEnergyProvider(
            tron_client=SequencedResourceTronClient(
                [
                    {
                        "EnergyLimit": 0,
                        "EnergyUsed": 0,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                    {
                        "EnergyLimit": 65_000,
                        "EnergyUsed": 0,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                ]
            )
        )
        created_orders = []
        provider._create_order = lambda settings, receiver, amount: created_orders.append(
            (receiver, amount)
        ) or {"id": "order-1", "status": "pending"}
        provider._wait_until_delegated = lambda settings, order_id, order: {
            "id": order_id,
            "status": "delegated",
        }

        original_config = __import__("app.resource_providers.refee", fromlist=["config"]).config
        __import__("app.resource_providers.refee", fromlist=["config"]).config = SimpleNamespace(
            REFEE=FakeSettings(),
            REFEE_FIXED_ENERGY_ORDER_AMOUNT=65_000,
        )
        try:
            acquired = provider.acquire_energy(
                ONETIME,
                72_321,
                {},
                minimum_energy_required=72_321,
            )
        finally:
            __import__("app.resource_providers.refee", fromlist=["config"]).config = original_config

        self.assertTrue(acquired)
        self.assertEqual(created_orders, [(ONETIME, 65_000)])

    def test_refee_provider_strict_minimum_orders_above_fixed_amount(self):
        from app.resource_providers.refee import RefeeProvider

        provider = RefeeProvider(
            tron_client=SequencedResourceTronClient(
                [
                    {
                        "EnergyLimit": 0,
                        "EnergyUsed": 0,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                    {
                        "EnergyLimit": 131_000,
                        "EnergyUsed": 0,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                ]
            )
        )
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
                min_energy_order_amount=30_000,
                api_base_url="https://api.refee.bot",
                api_key=SimpleNamespace(get_secret_value=lambda: "token"),
                poll_interval_sec=0.01,
                timeout_sec=1,
            ),
            REFEE_FIXED_ENERGY_ORDER_AMOUNT=65_000,
        )
        try:
            acquired = provider.acquire_energy(
                ONETIME,
                131_000,
                {},
                minimum_energy_required=131_000,
                strict_minimum_required=True,
            )
        finally:
            module.config = original_config

        self.assertTrue(acquired)
        self.assertEqual(created_orders, [(ONETIME, 131_000)])

    def test_refee_provider_energy_recheck_failure_marks_order_accepted(self):
        from app.resource_providers.refee import RefeeProvider

        class FakeSettings:
            energy_overprovision_factor = Decimal("1.05")
            min_energy_order_amount = 30_000
            rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01

        provider = RefeeProvider(
            tron_client=SequencedResourceTronClient(
                [
                    {
                        "EnergyLimit": 0,
                        "EnergyUsed": 0,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                    {
                        "EnergyLimit": 0,
                        "EnergyUsed": 0,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                ]
            )
        )
        provider._create_order = lambda settings, receiver, amount: {
            "id": "order-1",
            "status": "pending",
        }
        provider._wait_until_delegated = lambda settings, order_id, order: {
            "id": order_id,
            "status": "delegated",
        }

        module = __import__("app.resource_providers.refee", fromlist=["config"])
        original_config = module.config
        module.config = SimpleNamespace(
            REFEE=FakeSettings(),
            REFEE_FIXED_ENERGY_ORDER_AMOUNT=65_000,
        )
        try:
            acquired = provider.acquire_energy(
                ONETIME,
                65_000,
                {},
                minimum_energy_required=65_000,
                strict_minimum_required=True,
            )
        finally:
            module.config = original_config

        self.assertFalse(acquired)
        self.assertEqual(provider.last_failure.code, "RESOURCE_RECHECK_FAILED")
        self.assertTrue(provider.last_failure.temporary)
        self.assertFalse(provider.last_failure.fallback_eligible)
        self.assertTrue(provider.last_failure.order_accepted)
        self.assertEqual(provider.last_failure.task_id, "order-1")

    def test_refee_provider_malformed_accepted_energy_order_marks_order_accepted(self):
        from app.resource_providers.refee import RefeeProvider

        class FakeSettings:
            energy_overprovision_factor = Decimal("1.05")
            min_energy_order_amount = 30_000
            rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01
            api_base_url = "https://api.refee.bot"
            api_key = SimpleNamespace(get_secret_value=lambda: "token")

        class Response:
            status_code = 202
            text = "accepted"

            def __init__(self, body=None, *, invalid_json=False):
                self.body = body
                self.invalid_json = invalid_json

            def json(self):
                if self.invalid_json:
                    raise ValueError("invalid json")
                return self.body

        for response in (Response(invalid_json=True), Response([])):
            with self.subTest(response=response):
                provider = RefeeProvider(
                    tron_client=SequencedResourceTronClient(
                        [
                            {
                                "EnergyLimit": 0,
                                "EnergyUsed": 0,
                                "freeNetLimit": 0,
                                "freeNetUsed": 0,
                                "NetLimit": 0,
                                "NetUsed": 0,
                            }
                        ]
                    )
                )
                module = __import__("app.resource_providers.refee", fromlist=["config"])
                original_config = module.config
                original_requests = module.requests
                module.config = SimpleNamespace(
                    REFEE=FakeSettings(),
                    REFEE_FIXED_ENERGY_ORDER_AMOUNT=65_000,
                )
                module.requests = SimpleNamespace(post=lambda *args, **kwargs: response)
                try:
                    acquired = provider.acquire_energy(
                        ONETIME,
                        65_000,
                        {},
                        minimum_energy_required=65_000,
                        strict_minimum_required=True,
                    )
                finally:
                    module.config = original_config
                    module.requests = original_requests

                self.assertFalse(acquired)
                self.assertEqual(
                    provider.last_failure.code,
                    "ACCEPTED_MALFORMED_RESPONSE",
                )
                self.assertTrue(provider.last_failure.temporary)
                self.assertFalse(provider.last_failure.fallback_eligible)
                self.assertTrue(provider.last_failure.order_accepted)
                self.assertIsNone(provider.last_failure.task_id)

    def test_refee_provider_create_order_classifies_http_failures(self):
        from app.resource_providers.refee import RefeeProvider

        class FakeSettings:
            rent_duration_label = "1h"
            api_base_url = "https://api.refee.bot"
            api_key = SimpleNamespace(get_secret_value=lambda: "token")

        class Response:
            def __init__(self, status_code):
                self.status_code = status_code
                self.text = f"status {status_code}"

        cases = [
            (400, "INVALID_PARAMETERS", False, False),
            (401, "CONFIGURATION_ERROR", False, False),
            (402, "INSUFFICIENT_BALANCE", True, False),
            (403, "CONFIGURATION_ERROR", False, False),
            (408, "SERVICE_UNAVAILABLE", True, True),
            (409, "UNKNOWN_ERROR", True, False),
            (422, "CONFIGURATION_ERROR", False, False),
            (429, "SERVICE_UNAVAILABLE", True, True),
            (503, "SERVICE_UNAVAILABLE", True, True),
        ]
        module = __import__("app.resource_providers.refee", fromlist=["requests"])
        original_requests = module.requests
        try:
            for status_code, code, temporary, fallback_eligible in cases:
                with self.subTest(status_code=status_code):
                    provider = RefeeProvider()
                    module.requests = SimpleNamespace(
                        post=lambda *args, **kwargs: Response(status_code)
                    )

                    order = provider._create_order(
                        FakeSettings(),
                        ONETIME,
                        65_000,
                    )

                    self.assertIsNone(order)
                    self.assertEqual(provider.last_failure.code, code)
                    self.assertEqual(provider.last_failure.temporary, temporary)
                    self.assertEqual(
                        provider.last_failure.fallback_eligible,
                        fallback_eligible,
                    )
                    self.assertFalse(provider.last_failure.order_accepted)
                    self.assertIsNone(provider.last_failure.task_id)
        finally:
            module.requests = original_requests

    def test_refee_provider_accepts_chain_rounding_below_fixed_order_amount(self):
        from app.energy_provider import RefeeEnergyProvider

        class FakeSettings:
            energy_overprovision_factor = Decimal("1.01")
            min_energy_order_amount = 30_000
            rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01

        provider = RefeeEnergyProvider(
            tron_client=SequencedResourceTronClient(
                [
                    {
                        "EnergyLimit": 0,
                        "EnergyUsed": 0,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                    {
                        "EnergyLimit": 64_999,
                        "EnergyUsed": 0,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                ]
            )
        )
        created_orders = []
        provider._create_order = lambda settings, receiver, amount: created_orders.append(
            (receiver, amount)
        ) or {"id": "order-1", "status": "pending"}
        provider._wait_until_delegated = lambda settings, order_id, order: {
            "id": order_id,
            "status": "delegated",
        }

        original_config = __import__("app.resource_providers.refee", fromlist=["config"]).config
        __import__("app.resource_providers.refee", fromlist=["config"]).config = SimpleNamespace(
            REFEE=FakeSettings(),
            REFEE_FIXED_ENERGY_ORDER_AMOUNT=65_000,
        )
        try:
            acquired = provider.acquire_energy(
                ONETIME,
                64_285,
                {},
                minimum_energy_required=64_285,
            )
        finally:
            __import__("app.resource_providers.refee", fromlist=["config"]).config = original_config

        self.assertTrue(acquired)
        self.assertEqual(created_orders, [(ONETIME, 65_000)])

    def test_refee_provider_skips_duplicate_fixed_order_after_chain_rounding(self):
        from app.energy_provider import RefeeEnergyProvider

        class FakeSettings:
            energy_overprovision_factor = Decimal("1.01")
            min_energy_order_amount = 30_000
            rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01

        provider = RefeeEnergyProvider(
            tron_client=FakeTronClient(
                {
                    "EnergyLimit": 64_999,
                    "EnergyUsed": 0,
                    "freeNetLimit": 0,
                    "freeNetUsed": 0,
                    "NetLimit": 0,
                    "NetUsed": 0,
                }
            )
        )
        created_orders = []
        provider._create_order = lambda settings, receiver, amount: created_orders.append(
            (receiver, amount)
        ) or {"id": "order-1", "status": "pending"}
        provider._wait_until_delegated = lambda settings, order_id, order: {
            "id": order_id,
            "status": "delegated",
        }

        original_config = __import__("app.resource_providers.refee", fromlist=["config"]).config
        __import__("app.resource_providers.refee", fromlist=["config"]).config = SimpleNamespace(
            REFEE=FakeSettings(),
            REFEE_FIXED_ENERGY_ORDER_AMOUNT=65_000,
        )
        try:
            acquired = provider.acquire_energy(
                ONETIME,
                7_322,
                {},
                minimum_energy_required=72_321,
            )
        finally:
            __import__("app.resource_providers.refee", fromlist=["config"]).config = original_config

        self.assertTrue(acquired)
        self.assertEqual(created_orders, [])

    def test_refee_provider_accepts_fixed_order_tolerance_lower_bound(self):
        from app.energy_provider import RefeeEnergyProvider

        class FakeSettings:
            energy_overprovision_factor = Decimal("1.01")
            min_energy_order_amount = 30_000
            rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01

        provider = RefeeEnergyProvider(
            tron_client=SequencedResourceTronClient(
                [
                    {
                        "EnergyLimit": 0,
                        "EnergyUsed": 0,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                    {
                        "EnergyLimit": 64_500,
                        "EnergyUsed": 0,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                ]
            )
        )
        created_orders = []
        provider._create_order = lambda settings, receiver, amount: created_orders.append(
            (receiver, amount)
        ) or {"id": "order-1", "status": "pending"}
        provider._wait_until_delegated = lambda settings, order_id, order: {
            "id": order_id,
            "status": "delegated",
        }

        original_config = __import__("app.resource_providers.refee", fromlist=["config"]).config
        __import__("app.resource_providers.refee", fromlist=["config"]).config = SimpleNamespace(
            REFEE=FakeSettings(),
            REFEE_FIXED_ENERGY_ORDER_AMOUNT=65_000,
        )
        try:
            acquired = provider.acquire_energy(
                ONETIME,
                64_285,
                {},
                minimum_energy_required=64_285,
            )
        finally:
            __import__("app.resource_providers.refee", fromlist=["config"]).config = original_config

        self.assertTrue(acquired)
        self.assertEqual(created_orders, [(ONETIME, 65_000)])

    def test_refee_provider_rejects_fixed_order_below_tolerance_lower_bound(self):
        from app.energy_provider import RefeeEnergyProvider

        class FakeSettings:
            energy_overprovision_factor = Decimal("1.01")
            min_energy_order_amount = 30_000
            rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01

        provider = RefeeEnergyProvider(
            tron_client=SequencedResourceTronClient(
                [
                    {
                        "EnergyLimit": 0,
                        "EnergyUsed": 0,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                    {
                        "EnergyLimit": 64_499,
                        "EnergyUsed": 0,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                ]
            )
        )
        created_orders = []
        provider._create_order = lambda settings, receiver, amount: created_orders.append(
            (receiver, amount)
        ) or {"id": "order-1", "status": "pending"}
        provider._wait_until_delegated = lambda settings, order_id, order: {
            "id": order_id,
            "status": "delegated",
        }

        original_config = __import__("app.resource_providers.refee", fromlist=["config"]).config
        __import__("app.resource_providers.refee", fromlist=["config"]).config = SimpleNamespace(
            REFEE=FakeSettings(),
            REFEE_FIXED_ENERGY_ORDER_AMOUNT=65_000,
        )
        try:
            acquired = provider.acquire_energy(
                ONETIME,
                64_285,
                {},
                minimum_energy_required=64_285,
            )
        finally:
            __import__("app.resource_providers.refee", fromlist=["config"]).config = original_config

        self.assertFalse(acquired)
        self.assertEqual(created_orders, [(ONETIME, 65_000)])

    def test_refee_provider_dynamic_mode_when_fixed_order_amount_is_zero(self):
        from app.energy_provider import RefeeEnergyProvider

        class FakeSettings:
            energy_overprovision_factor = Decimal("1.05")
            min_energy_order_amount = 30_000
            rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01

        provider = RefeeEnergyProvider(
            tron_client=SequencedResourceTronClient(
                [
                    {
                        "EnergyLimit": 100_000,
                        "EnergyUsed": 50_000,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                    {
                        "EnergyLimit": 100_000,
                        "EnergyUsed": 20_000,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                ]
            )
        )
        created_orders = []
        provider._create_order = lambda settings, receiver, amount: created_orders.append(
            (receiver, amount)
        ) or {"id": "order-1", "status": "pending"}
        provider._wait_until_delegated = lambda settings, order_id, order: {
            "id": order_id,
            "status": "delegated",
        }

        original_config = __import__("app.resource_providers.refee", fromlist=["config"]).config
        __import__("app.resource_providers.refee", fromlist=["config"]).config = SimpleNamespace(
            REFEE=FakeSettings(),
            REFEE_FIXED_ENERGY_ORDER_AMOUNT=0,
        )
        try:
            acquired = provider.acquire_energy(
                ONETIME,
                30_000,
                {},
                minimum_energy_required=80_000,
            )
        finally:
            __import__("app.resource_providers.refee", fromlist=["config"]).config = original_config

        self.assertTrue(acquired)
        self.assertEqual(created_orders, [(ONETIME, 31_500)])

    def test_refee_provider_fixed_mode_skips_order_when_energy_already_available(self):
        from app.energy_provider import RefeeEnergyProvider

        class FakeSettings:
            energy_overprovision_factor = Decimal("1.01")
            min_energy_order_amount = 30_000
            rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01

        provider = RefeeEnergyProvider(
            tron_client=FakeTronClient(
                {
                    "EnergyLimit": 70_000,
                    "EnergyUsed": 0,
                    "freeNetLimit": 0,
                    "freeNetUsed": 0,
                    "NetLimit": 0,
                    "NetUsed": 0,
                }
            )
        )
        created_orders = []
        provider._create_order = lambda settings, receiver, amount: created_orders.append(
            (receiver, amount)
        ) or {"id": "order-1", "status": "pending"}

        original_config = __import__("app.resource_providers.refee", fromlist=["config"]).config
        __import__("app.resource_providers.refee", fromlist=["config"]).config = SimpleNamespace(
            REFEE=FakeSettings(),
            REFEE_FIXED_ENERGY_ORDER_AMOUNT=65_000,
        )
        try:
            acquired = provider.acquire_energy(
                ONETIME,
                72_321,
                {},
                minimum_energy_required=72_321,
            )
        finally:
            __import__("app.resource_providers.refee", fromlist=["config"]).config = original_config

        self.assertTrue(acquired)
        self.assertEqual(created_orders, [])

    def test_refee_provider_skips_new_order_when_receiver_already_has_required_energy(self):
        from app.energy_provider import RefeeEnergyProvider

        class FakeSettings:
            energy_overprovision_factor = Decimal("1.05")
            min_energy_order_amount = 30_000
            rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01

        provider = RefeeEnergyProvider(
            tron_client=FakeTronClient(
                {
                    "EnergyLimit": 80_000,
                    "EnergyUsed": 0,
                    "freeNetLimit": 0,
                    "freeNetUsed": 0,
                    "NetLimit": 0,
                    "NetUsed": 0,
                }
            )
        )
        created_orders = []
        provider._create_order = lambda settings, receiver, amount: created_orders.append(
            (receiver, amount)
        ) or {"id": "order-1", "status": "pending"}
        provider._wait_until_delegated = lambda settings, order_id, order: {
            "id": order_id,
            "status": "delegated",
        }

        original_config = __import__("app.resource_providers.refee", fromlist=["config"]).config
        __import__("app.resource_providers.refee", fromlist=["config"]).config = SimpleNamespace(
            REFEE=FakeSettings()
        )
        try:
            acquired = provider.acquire_energy(
                ONETIME,
                30_000,
                {},
                minimum_energy_required=80_000,
            )
        finally:
            __import__("app.resource_providers.refee", fromlist=["config"]).config = original_config

        self.assertTrue(acquired)
        self.assertEqual(created_orders, [])

    def test_refee_provider_recalculates_order_from_fresh_missing_energy(self):
        from app.energy_provider import RefeeEnergyProvider

        class FakeSettings:
            energy_overprovision_factor = Decimal("1.05")
            min_energy_order_amount = 30_000
            rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01

        provider = RefeeEnergyProvider(
            tron_client=SequencedResourceTronClient(
                [
                    {
                        "EnergyLimit": 80_000,
                        "EnergyUsed": 0,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                    {
                        "EnergyLimit": 100_000,
                        "EnergyUsed": 0,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                ]
            )
        )
        created_orders = []
        provider._create_order = lambda settings, receiver, amount: created_orders.append(
            (receiver, amount)
        ) or {"id": "order-1", "status": "pending"}
        provider._wait_until_delegated = lambda settings, order_id, order: {
            "id": order_id,
            "status": "delegated",
        }

        original_config = __import__("app.resource_providers.refee", fromlist=["config"]).config
        __import__("app.resource_providers.refee", fromlist=["config"]).config = SimpleNamespace(
            REFEE=FakeSettings()
        )
        try:
            acquired = provider.acquire_energy(
                ONETIME,
                100_000,
                {},
                minimum_energy_required=100_000,
            )
        finally:
            __import__("app.resource_providers.refee", fromlist=["config"]).config = original_config

        self.assertTrue(acquired)
        self.assertEqual(created_orders, [(ONETIME, 30_000)])

    def test_refee_provider_applies_live_api_minimum_energy_order_amount(self):
        from app.energy_provider import RefeeEnergyProvider

        class FakeSettings:
            energy_overprovision_factor = Decimal("1.05")
            min_energy_order_amount = 30_000
            rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01

        provider = RefeeEnergyProvider(
            tron_client=SequencedResourceTronClient(
                [
                    {
                        "EnergyLimit": 80_000,
                        "EnergyUsed": 20_000,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                    {
                        "EnergyLimit": 80_000,
                        "EnergyUsed": 10_000,
                        "freeNetLimit": 0,
                        "freeNetUsed": 0,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                ]
            )
        )
        created_orders = []
        provider._create_order = lambda settings, receiver, amount: created_orders.append(
            (receiver, amount)
        ) or {"id": "order-1", "status": "pending"}
        provider._wait_until_delegated = lambda settings, order_id, order: {
            "id": order_id,
            "status": "delegated",
        }

        original_config = __import__("app.resource_providers.refee", fromlist=["config"]).config
        __import__("app.resource_providers.refee", fromlist=["config"]).config = SimpleNamespace(
            REFEE=FakeSettings()
        )
        try:
            acquired = provider.acquire_energy(
                ONETIME,
                10_000,
                {},
                minimum_energy_required=70_000,
            )
        finally:
            __import__("app.resource_providers.refee", fromlist=["config"]).config = original_config

        self.assertTrue(acquired)
        self.assertEqual(created_orders, [(ONETIME, 30_000)])

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

        original_requests = __import__(
            "app.resource_providers.refee", fromlist=["requests"]
        ).requests
        original_config = __import__(
            "app.resource_providers.refee", fromlist=["config"]
        ).config
        __import__(
            "app.resource_providers.refee", fromlist=["requests"]
        ).requests = SimpleNamespace(get=fake_get)
        __import__(
            "app.resource_providers.refee", fromlist=["config"]
        ).config = SimpleNamespace(
            REFEE=SimpleNamespace(
                api_base_url="https://api.refee.bot",
                api_key=SimpleNamespace(get_secret_value=lambda: "token"),
            )
        )
        try:
            estimate = provider.estimate_usdt_transfer_fee("TSourceAddress")
        finally:
            __import__(
                "app.resource_providers.refee", fromlist=["requests"]
            ).requests = original_requests
            __import__(
                "app.resource_providers.refee", fromlist=["config"]
            ).config = original_config

        self.assertEqual(
            estimate,
            {
                "energy_required": 65000,
                "is_new_address": False,
                "trx_burned": None,
                "provider": "refee",
            },
        )
        self.assertEqual(
            calls[0][0], "https://api.refee.bot/api/functions/cost/TSourceAddress"
        )
        self.assertEqual(calls[0][1]["X-API-Key"], "token")
        self.assertEqual(calls[0][2], provider.REQUEST_TIMEOUT_SEC)

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

        for body in ({}, [], {"cost": "65000"}, {"cost": 0}, {"cost": -1}):
            with self.subTest(body=body):
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
                    self.assertIsNone(
                        provider.estimate_usdt_transfer_fee("TSourceAddress")
                    )
                finally:
                    module.requests = original_requests
                    module.config = original_config

    def test_refee_provider_returns_none_when_refee_config_missing(self):
        from app.resource_providers.refee import RefeeProvider

        module = __import__("app.resource_providers.refee", fromlist=["config"])
        original_config = module.config
        module.config = SimpleNamespace(REFEE=None)
        try:
            self.assertIsNone(
                RefeeProvider().estimate_usdt_transfer_fee("TSourceAddress")
            )
        finally:
            module.config = original_config

    def test_refee_provider_returns_none_when_estimate_request_fails(self):
        from app.resource_providers.refee import RefeeProvider

        module = __import__("app.resource_providers.refee", fromlist=["requests"])
        original_requests = module.requests
        original_config = module.config

        def fake_get(url, headers=None, timeout=None):
            raise original_requests.RequestException("network failed")

        module.requests = SimpleNamespace(
            get=fake_get,
            RequestException=original_requests.RequestException,
        )
        module.config = SimpleNamespace(
            REFEE=SimpleNamespace(
                api_base_url="https://api.refee.bot",
                api_key=SimpleNamespace(get_secret_value=lambda: "token"),
            )
        )
        try:
            self.assertIsNone(
                RefeeProvider().estimate_usdt_transfer_fee("TSourceAddress")
            )
        finally:
            module.requests = original_requests
            module.config = original_config

    def test_refee_provider_returns_none_when_estimate_response_is_not_accepted(self):
        from app.resource_providers.refee import RefeeProvider

        class Response:
            status_code = 503
            text = "service unavailable"

            def json(self):
                return {"cost": 65000}

        module = __import__("app.resource_providers.refee", fromlist=["requests"])
        original_requests = module.requests
        original_config = module.config
        module.requests = SimpleNamespace(get=lambda url, headers=None, timeout=None: Response())
        module.config = SimpleNamespace(
            REFEE=SimpleNamespace(
                api_base_url="https://api.refee.bot",
                api_key=SimpleNamespace(get_secret_value=lambda: "token"),
            )
        )
        try:
            self.assertIsNone(
                RefeeProvider().estimate_usdt_transfer_fee("TSourceAddress")
            )
        finally:
            module.requests = original_requests
            module.config = original_config

    def test_refee_provider_returns_none_when_estimate_response_json_is_invalid(self):
        from app.resource_providers.refee import RefeeProvider

        class Response:
            status_code = 200
            text = "not json"

            def json(self):
                raise ValueError("invalid json")

        module = __import__("app.resource_providers.refee", fromlist=["requests"])
        original_requests = module.requests
        original_config = module.config
        module.requests = SimpleNamespace(get=lambda url, headers=None, timeout=None: Response())
        module.config = SimpleNamespace(
            REFEE=SimpleNamespace(
                api_base_url="https://api.refee.bot",
                api_key=SimpleNamespace(get_secret_value=lambda: "token"),
            )
        )
        try:
            self.assertIsNone(
                RefeeProvider().estimate_usdt_transfer_fee("TSourceAddress")
            )
        finally:
            module.requests = original_requests
            module.config = original_config


if __name__ == "__main__":
    unittest.main()

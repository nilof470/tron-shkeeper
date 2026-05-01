from decimal import Decimal
from types import SimpleNamespace
import unittest


FEE_DEPOSIT = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
ONETIME = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"


class FakeConfig:
    ENERGY_SOURCE = "refee"
    ENERGY_DELEGATION_MODE = False
    ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT = False
    ENERGY_DELEGATION_MODE_ALLOW_ADDITIONAL_ENERGY_DELEGATION = False
    BANDWIDTH_PER_TRC20_TRANSFER_CALL = 346
    TX_FEE_LIMIT = Decimal("50")

    def get_contract_address(self, _symbol):
        return "TCONTRACT"

    def get_min_transfer_threshold(self, _symbol):
        return Decimal("1")


class FakeStakingConfig(FakeConfig):
    ENERGY_SOURCE = "staking"
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


class RecordingProvider:
    def __init__(self, acquire_result=False):
        self.acquire_result = acquire_result
        self.acquire_calls = []
        self.release_calls = []

    def acquire(self, *args, **kwargs):
        self.acquire_calls.append((args, kwargs))
        return self.acquire_result

    def release(self, receiver):
        self.release_calls.append(receiver)


class RefeeEnergyAccountingTests(unittest.TestCase):
    def patch_tasks(self, tasks, client, provider, config=None):
        original_config = tasks.config
        original_connection_manager = tasks.ConnectionManager
        original_get_key = tasks.get_key
        original_get_energy_delegator = tasks.get_energy_delegator
        original_get_energy_provider = tasks.get_energy_provider

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
        tasks.get_energy_provider = lambda tron_client=None: provider

        def restore():
            tasks.config = original_config
            tasks.ConnectionManager = original_connection_manager
            tasks.get_key = original_get_key
            tasks.get_energy_delegator = original_get_energy_delegator
            tasks.get_energy_provider = original_get_energy_provider

        return restore

    def test_refee_acquires_missing_energy_when_existing_energy_is_partly_used(self):
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
        restore = self.patch_tasks(tasks, client, provider)
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

    def test_refee_rents_energy_when_only_bandwidth_is_already_delegated(self):
        from app import tasks

        provider = RecordingProvider(acquire_result=False)
        client = FakeTronClient(
            {
                "EnergyLimit": 0,
                "EnergyUsed": 0,
                "freeNetLimit": 600,
                "freeNetUsed": 0,
                "NetLimit": 999,
                "NetUsed": 0,
            },
            delegated_resource_index={"fromAccounts": ["TBANDWIDTH"]},
        )
        restore = self.patch_tasks(tasks, client, provider)
        try:
            result = tasks.transfer_trc20_from.run(ONETIME, "USDT")
        finally:
            restore()

        self.assertIsNone(result)
        self.assertEqual(len(provider.acquire_calls), 1)
        args, kwargs = provider.acquire_calls[0]
        self.assertEqual(args[1], 50_000)
        self.assertEqual(kwargs["minimum_energy_required"], 50_000)

    def test_failed_trc20_receipt_is_not_treated_as_successful_sweep(self):
        from app import tasks

        provider = RecordingProvider(acquire_result=True)
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
        restore = self.patch_tasks(tasks, client, provider)
        try:
            result = tasks.transfer_trc20_from.run(ONETIME, "USDT")
        finally:
            restore()

        self.assertIsNone(result)
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

        original_config = __import__("app.energy_provider").energy_provider.config
        __import__("app.energy_provider").energy_provider.config = SimpleNamespace(
            REFEE=FakeSettings()
        )
        try:
            acquired = provider.acquire(
                ONETIME,
                30_000,
                {},
                minimum_energy_required=80_000,
            )
        finally:
            __import__("app.energy_provider").energy_provider.config = original_config

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

        original_config = __import__("app.energy_provider").energy_provider.config
        __import__("app.energy_provider").energy_provider.config = SimpleNamespace(
            REFEE=FakeSettings(),
            REFEE_FIXED_ENERGY_ORDER_AMOUNT=65_000,
        )
        try:
            acquired = provider.acquire(
                ONETIME,
                72_321,
                {},
                minimum_energy_required=72_321,
            )
        finally:
            __import__("app.energy_provider").energy_provider.config = original_config

        self.assertTrue(acquired)
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

        original_config = __import__("app.energy_provider").energy_provider.config
        __import__("app.energy_provider").energy_provider.config = SimpleNamespace(
            REFEE=FakeSettings(),
            REFEE_FIXED_ENERGY_ORDER_AMOUNT=0,
        )
        try:
            acquired = provider.acquire(
                ONETIME,
                30_000,
                {},
                minimum_energy_required=80_000,
            )
        finally:
            __import__("app.energy_provider").energy_provider.config = original_config

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

        original_config = __import__("app.energy_provider").energy_provider.config
        __import__("app.energy_provider").energy_provider.config = SimpleNamespace(
            REFEE=FakeSettings(),
            REFEE_FIXED_ENERGY_ORDER_AMOUNT=65_000,
        )
        try:
            acquired = provider.acquire(
                ONETIME,
                72_321,
                {},
                minimum_energy_required=72_321,
            )
        finally:
            __import__("app.energy_provider").energy_provider.config = original_config

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

        original_config = __import__("app.energy_provider").energy_provider.config
        __import__("app.energy_provider").energy_provider.config = SimpleNamespace(
            REFEE=FakeSettings()
        )
        try:
            acquired = provider.acquire(
                ONETIME,
                30_000,
                {},
                minimum_energy_required=80_000,
            )
        finally:
            __import__("app.energy_provider").energy_provider.config = original_config

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

        original_config = __import__("app.energy_provider").energy_provider.config
        __import__("app.energy_provider").energy_provider.config = SimpleNamespace(
            REFEE=FakeSettings()
        )
        try:
            acquired = provider.acquire(
                ONETIME,
                100_000,
                {},
                minimum_energy_required=100_000,
            )
        finally:
            __import__("app.energy_provider").energy_provider.config = original_config

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

        original_config = __import__("app.energy_provider").energy_provider.config
        __import__("app.energy_provider").energy_provider.config = SimpleNamespace(
            REFEE=FakeSettings()
        )
        try:
            acquired = provider.acquire(
                ONETIME,
                10_000,
                {},
                minimum_energy_required=70_000,
            )
        finally:
            __import__("app.energy_provider").energy_provider.config = original_config

        self.assertTrue(acquired)
        self.assertEqual(created_orders, [(ONETIME, 30_000)])


if __name__ == "__main__":
    unittest.main()

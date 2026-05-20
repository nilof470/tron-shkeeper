from decimal import Decimal
from types import SimpleNamespace
import unittest


class FakeContractFunctions:
    def decimals(self):
        return 6

    def balanceOf(self, _address):
        return 3_000_000


class FakeContract:
    functions = FakeContractFunctions()


class FakeTronClient:
    def __init__(self):
        self.energy_estimate_calls = 0

    def get_contract(self, _contract_address):
        return FakeContract()

    def get_account_resource(self, _address):
        return {
            "EnergyLimit": 0,
            "freeNetLimit": 600,
            "freeNetUsed": 600,
            "NetLimit": 0,
            "NetUsed": 0,
        }

    def get_estimated_energy(self, *_args, **_kwargs):
        self.energy_estimate_calls += 1
        return 100_000

    def get_delegated_resource_account_index_v2(self, _address):
        return {}


class SufficientBandwidthTronClient(FakeTronClient):
    def get_account_resource(self, _address):
        return {
            "EnergyLimit": 0,
            "freeNetLimit": 600,
            "freeNetUsed": 0,
            "NetLimit": 0,
            "NetUsed": 0,
        }


class FakeProvider:
    def __init__(self):
        self.acquire_calls = 0
        self.acquire_bandwidth_calls = []

    def acquire_energy(self, *_args, **_kwargs):
        self.acquire_calls += 1
        return False

    def acquire_bandwidth(self, receiver, bandwidth_required):
        self.acquire_bandwidth_calls.append((receiver, bandwidth_required))
        return True


class FakeConfig:
    ENERGY_PROVIDER = "refee"
    BANDWIDTH_PROVIDER = "refee"
    ENERGY_DELEGATION_MODE = False
    ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT = False
    ENERGY_DELEGATION_MODE_ALLOW_ADDITIONAL_ENERGY_DELEGATION = False
    BANDWIDTH_PER_TRC20_TRANSFER_CALL = 346

    def get_contract_address(self, symbol):
        self.last_contract_symbol = symbol
        return "TCONTRACT"

    def get_min_transfer_threshold(self, symbol):
        self.last_threshold_symbol = symbol
        return Decimal("1")


class DisabledBandwidthProviderConfig(FakeConfig):
    BANDWIDTH_PROVIDER = "disabled"


class StakingEnergyRefeeBandwidthConfig(FakeConfig):
    ENERGY_PROVIDER = "staking"
    BANDWIDTH_PROVIDER = "refee"
    ENERGY_DELEGATION_MODE = True
    ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH = True
    BANDWIDTH_PER_DELEGE_CALL = 1
    BANDWIDTH_PER_UNDELEGATE_CALL = 1
    BANDWIDTH_PER_TRX_TRANSFER = 1


class FakeEnergyProviderWithoutBandwidth:
    def __init__(self):
        self.acquire_calls = 0

    def acquire_energy(self, *_args, **_kwargs):
        self.acquire_calls += 1
        return False


class RefeeBandwidthGuardTests(unittest.TestCase):
    def test_refee_sweep_rents_bandwidth_before_acquiring_energy(self):
        from app import tasks
        from app.schemas import KeyType

        fee_deposit = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
        onetime = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
        client = FakeTronClient()
        provider = FakeProvider()
        original_config = tasks.config
        original_connection_manager = tasks.ConnectionManager
        original_get_key = tasks.get_key
        original_get_energy_provider = tasks.get_energy_provider
        original_get_bandwidth_provider = tasks.get_bandwidth_provider
        try:
            tasks.config = FakeConfig()
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

        self.assertIsNone(result)
        self.assertEqual(
            provider.acquire_bandwidth_calls,
            [(onetime, FakeConfig.BANDWIDTH_PER_TRC20_TRANSFER_CALL)],
        )
        self.assertEqual(provider.acquire_calls, 1)
        self.assertEqual(client.energy_estimate_calls, 1)

    def test_staking_energy_uses_separate_refee_bandwidth_provider(self):
        from app import tasks
        from app.schemas import KeyType

        fee_deposit = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
        onetime = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
        client = FakeTronClient()
        energy_provider = FakeEnergyProviderWithoutBandwidth()
        bandwidth_provider = FakeProvider()
        original_config = tasks.config
        original_connection_manager = tasks.ConnectionManager
        original_get_key = tasks.get_key
        original_get_energy_delegator = tasks.get_energy_delegator
        original_get_energy_provider = tasks.get_energy_provider
        original_get_bandwidth_provider = tasks.get_bandwidth_provider
        try:
            tasks.config = StakingEnergyRefeeBandwidthConfig()
            tasks.ConnectionManager = SimpleNamespace(client=lambda: client)

            def fake_get_key(key_type, pub=None):
                if key_type == KeyType.fee_deposit:
                    return object(), fee_deposit
                if key_type == KeyType.onetime:
                    return object(), pub
                raise AssertionError(f"unexpected key type {key_type}")

            tasks.get_key = fake_get_key
            tasks.get_energy_delegator = lambda: (object(), "TDELEGATOR")
            tasks.get_energy_provider = lambda tron_client=None: energy_provider
            tasks.get_bandwidth_provider = lambda tron_client=None: bandwidth_provider

            result = tasks.transfer_trc20_from.run(onetime, "USDT")
        finally:
            tasks.config = original_config
            tasks.ConnectionManager = original_connection_manager
            tasks.get_key = original_get_key
            tasks.get_energy_delegator = original_get_energy_delegator
            tasks.get_energy_provider = original_get_energy_provider
            tasks.get_bandwidth_provider = original_get_bandwidth_provider

        self.assertIsNone(result)
        self.assertEqual(
            bandwidth_provider.acquire_bandwidth_calls,
            [(onetime, FakeConfig.BANDWIDTH_PER_TRC20_TRANSFER_CALL)],
        )
        self.assertEqual(energy_provider.acquire_calls, 1)
        self.assertEqual(client.energy_estimate_calls, 1)

    def test_sweep_uses_existing_bandwidth_only_when_bandwidth_provider_disabled(self):
        from app import tasks
        from app.schemas import KeyType

        fee_deposit = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
        onetime = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
        client = FakeTronClient()
        provider = FakeProvider()
        original_config = tasks.config
        original_connection_manager = tasks.ConnectionManager
        original_get_key = tasks.get_key
        original_get_energy_provider = tasks.get_energy_provider
        original_get_bandwidth_provider = tasks.get_bandwidth_provider
        try:
            tasks.config = DisabledBandwidthProviderConfig()
            tasks.ConnectionManager = SimpleNamespace(client=lambda: client)

            def fake_get_key(key_type, pub=None):
                if key_type == KeyType.fee_deposit:
                    return object(), fee_deposit
                if key_type == KeyType.onetime:
                    return object(), pub
                raise AssertionError(f"unexpected key type {key_type}")

            tasks.get_key = fake_get_key
            tasks.get_energy_provider = lambda tron_client=None: provider

            def fail_get_bandwidth_provider(tron_client=None):
                raise AssertionError("disabled bandwidth provider must not be requested")

            tasks.get_bandwidth_provider = fail_get_bandwidth_provider

            result = tasks.transfer_trc20_from.run(onetime, "USDT")
        finally:
            tasks.config = original_config
            tasks.ConnectionManager = original_connection_manager
            tasks.get_key = original_get_key
            tasks.get_energy_provider = original_get_energy_provider
            tasks.get_bandwidth_provider = original_get_bandwidth_provider

        self.assertIsNone(result)
        self.assertEqual(provider.acquire_bandwidth_calls, [])
        self.assertEqual(provider.acquire_calls, 0)
        self.assertEqual(client.energy_estimate_calls, 0)

    def test_refee_sweep_skips_bandwidth_rental_when_bandwidth_is_available(self):
        from app import tasks
        from app.schemas import KeyType

        fee_deposit = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
        onetime = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
        client = SufficientBandwidthTronClient()
        provider = FakeProvider()
        original_config = tasks.config
        original_connection_manager = tasks.ConnectionManager
        original_get_key = tasks.get_key
        original_get_energy_provider = tasks.get_energy_provider
        original_get_bandwidth_provider = tasks.get_bandwidth_provider
        try:
            tasks.config = FakeConfig()
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

        self.assertIsNone(result)
        self.assertEqual(provider.acquire_bandwidth_calls, [])
        self.assertEqual(provider.acquire_calls, 1)
        self.assertEqual(client.energy_estimate_calls, 1)

    def test_sweep_can_use_different_energy_and_bandwidth_providers(self):
        from app import tasks
        from app.schemas import KeyType

        class MixedProviderConfig(FakeConfig):
            ENERGY_PROVIDER = "refee"
            BANDWIDTH_PROVIDER = "profeex"

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
        try:
            tasks.config = MixedProviderConfig()
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
            [(onetime, MixedProviderConfig.BANDWIDTH_PER_TRC20_TRANSFER_CALL)],
        )
        self.assertEqual(energy_provider.acquire_calls, 1)
        self.assertEqual(client.energy_estimate_calls, 1)

    def test_refee_provider_rents_minimum_bandwidth_order(self):
        from app.resource_providers.refee import RefeeProvider

        class FakeSettings:
            min_bandwidth_order_amount = 1_000
            rent_duration_label = "1h"
            bandwidth_rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01

        class SequencedBandwidthTronClient:
            def __init__(self):
                self.resources = [
                    {
                        "freeNetLimit": 600,
                        "freeNetUsed": 600,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                    {
                        "freeNetLimit": 600,
                        "freeNetUsed": 600,
                        "NetLimit": 1_000,
                        "NetUsed": 0,
                    },
                ]

            def get_account_resource(self, _address):
                if len(self.resources) > 1:
                    return self.resources.pop(0)
                return self.resources[0]

        provider = RefeeProvider(tron_client=SequencedBandwidthTronClient())
        created_orders = []

        def fake_create_order(
            settings,
            receiver,
            amount,
            resource="energy",
            duration_label=None,
        ):
            created_orders.append((receiver, amount, resource, duration_label))
            return {"id": "order-1", "status": "pending"}

        provider._create_order = fake_create_order
        provider._wait_until_delegated = lambda settings, order_id, order: {
            "id": order_id,
            "status": "delegated",
        }

        original_config = __import__("app.resource_providers.refee", fromlist=["config"]).config
        __import__("app.resource_providers.refee", fromlist=["config"]).config = SimpleNamespace(
            REFEE=FakeSettings()
        )
        try:
            acquired = provider.acquire_bandwidth(
                "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7",
                346,
            )
        finally:
            __import__("app.resource_providers.refee", fromlist=["config"]).config = original_config

        self.assertTrue(acquired)
        self.assertEqual(
            created_orders,
            [("TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7", 1_000, "bandwidth", "1h")],
        )

    def test_refee_provider_uses_separate_bandwidth_duration(self):
        from app.resource_providers.refee import RefeeProvider

        class FakeSettings:
            min_bandwidth_order_amount = 1_000
            rent_duration_label = "3d"
            bandwidth_rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01

        class SequencedBandwidthTronClient:
            def __init__(self):
                self.resources = [
                    {
                        "freeNetLimit": 600,
                        "freeNetUsed": 600,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                    {
                        "freeNetLimit": 600,
                        "freeNetUsed": 600,
                        "NetLimit": 1_000,
                        "NetUsed": 0,
                    },
                ]

            def get_account_resource(self, _address):
                if len(self.resources) > 1:
                    return self.resources.pop(0)
                return self.resources[0]

        provider = RefeeProvider(tron_client=SequencedBandwidthTronClient())
        created_orders = []

        def fake_create_order(
            settings,
            receiver,
            amount,
            resource="energy",
            duration_label=None,
        ):
            created_orders.append((receiver, amount, resource, duration_label))
            return {"id": "order-1", "status": "pending"}

        provider._create_order = fake_create_order
        provider._wait_until_delegated = lambda settings, order_id, order: {
            "id": order_id,
            "status": "delegated",
        }

        original_config = __import__("app.resource_providers.refee", fromlist=["config"]).config
        __import__("app.resource_providers.refee", fromlist=["config"]).config = SimpleNamespace(
            REFEE=FakeSettings()
        )
        try:
            acquired = provider.acquire_bandwidth(
                "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7",
                346,
            )
        finally:
            __import__("app.resource_providers.refee", fromlist=["config"]).config = original_config

        self.assertTrue(acquired)
        self.assertEqual(
            created_orders,
            [("TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7", 1_000, "bandwidth", "1h")],
        )


if __name__ == "__main__":
    unittest.main()

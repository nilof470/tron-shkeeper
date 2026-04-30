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


class FakeProvider:
    def __init__(self):
        self.acquire_calls = 0

    def acquire(self, *_args, **_kwargs):
        self.acquire_calls += 1
        return True


class FakeConfig:
    ENERGY_SOURCE = "refee"
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


class RefeeBandwidthGuardTests(unittest.TestCase):
    def test_refee_sweep_checks_onetime_bandwidth_before_acquiring_energy(self):
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

            result = tasks.transfer_trc20_from.run(onetime, "USDT")
        finally:
            tasks.config = original_config
            tasks.ConnectionManager = original_connection_manager
            tasks.get_key = original_get_key
            tasks.get_energy_provider = original_get_energy_provider

        self.assertIsNone(result)
        self.assertEqual(provider.acquire_calls, 0)
        self.assertEqual(client.energy_estimate_calls, 0)


if __name__ == "__main__":
    unittest.main()

from decimal import Decimal
from contextlib import contextmanager
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
        self.acquire_energy_calls = []
        self.acquire_bandwidth_calls = []
        self.fee_estimate_calls = []

    def acquire_energy(self, *args, **kwargs):
        self.acquire_energy_calls.append((args, kwargs))
        self.acquire_calls += 1
        return False

    def acquire_bandwidth(self, receiver, bandwidth_required):
        self.acquire_bandwidth_calls.append((receiver, bandwidth_required))
        return True

    def estimate_usdt_transfer_fee(self, receiver_address):
        self.fee_estimate_calls.append(receiver_address)
        return {
            "energy_required": 64_285,
            "is_new_address": False,
            "trx_burned": "0",
        }


class FakeConfig:
    ENERGY_PROVIDER = "refee"
    BANDWIDTH_PROVIDER = "refee"
    ENERGY_DELEGATION_MODE = False
    ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT = False
    ENERGY_DELEGATION_MODE_ALLOW_ADDITIONAL_ENERGY_DELEGATION = False
    ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH = True
    BANDWIDTH_PER_TRC20_TRANSFER_CALL = 346
    BANDWIDTH_PER_TRX_TRANSFER = 1
    REFEE_FIXED_ENERGY_ORDER_AMOUNT = 65_000
    TX_FEE_LIMIT = Decimal("50")

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


class ProfeeXEnergyConfig(FakeConfig):
    ENERGY_PROVIDER = "profeex"
    BANDWIDTH_PROVIDER = "profeex"
    ENERGY_DELEGATION_MODE = False
    ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT = True
    TX_FEE_LIMIT = Decimal("50")


class FakeEnergyProviderWithoutBandwidth:
    def __init__(self):
        self.acquire_calls = 0

    def acquire_energy(self, *_args, **_kwargs):
        self.acquire_calls += 1
        return False


class OrderedEnergyProvider(FakeProvider):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def acquire_energy(self, *_args, **_kwargs):
        self.events.append("energy")
        return super().acquire_energy(*_args, **_kwargs)


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


class RecordingTrx:
    def __init__(self):
        self.transfers = []

    def transfer(self, src, dst, amount):
        self.transfers.append((src, dst, amount))
        return FakeTokenTransfer()


class InactiveSourceSweepTronClient(SuccessfulSweepTronClient):
    def __init__(self, inactive_address):
        self.inactive_address = inactive_address
        self.source_active = False
        self.trx = RecordingTrx()

    def get_account_balance(self, _address):
        return Decimal("2")

    def get_account_resource(self, address):
        if address == self.inactive_address and not self.source_active:
            import tronpy.exceptions

            raise tronpy.exceptions.AddressNotFound
        return super().get_account_resource(address)


class NoBroadcastContractFunctions(FakeContractFunctions):
    def transfer(self, _address, _amount):
        raise AssertionError("token transfer must not be built before resources are ready")


class NoBroadcastContract:
    functions = NoBroadcastContractFunctions()


class NoBroadcastTronClient(FakeTronClient):
    def get_contract(self, _contract_address):
        return NoBroadcastContract()


class RefeeBandwidthGuardTests(unittest.TestCase):
    def setUp(self):
        from app import tasks

        self._tasks = tasks
        self._original_is_sweep_allowed = tasks.is_sweep_allowed
        tasks.is_sweep_allowed = lambda *_args, **_kwargs: True

    def tearDown(self):
        self._tasks.is_sweep_allowed = self._original_is_sweep_allowed
        self._tasks = None
        self._original_is_sweep_allowed = None

    def test_refee_usdt_sweep_uses_shared_provisioning_before_broadcast(self):
        from app import tasks
        from app.schemas import KeyType

        fee_deposit = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
        onetime = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
        client = SuccessfulSweepTronClient()
        calls = []
        original_config = tasks.config
        original_connection_manager = tasks.ConnectionManager
        original_get_key = tasks.get_key
        original_get_energy_provider = tasks.get_energy_provider
        original_get_bandwidth_provider = tasks.get_bandwidth_provider
        original_ensure = tasks.ensure_usdt_transfer_resources
        original_estimate = tasks.estimate_trc20_sweep_energy
        try:
            tasks.config = FakeConfig()
            tasks.ConnectionManager = SimpleNamespace(client=lambda: client)

            def fake_get_key(key_type, pub=None):
                if key_type == KeyType.fee_deposit:
                    return object(), fee_deposit
                if key_type == KeyType.onetime:
                    return object(), pub
                raise AssertionError(f"unexpected key type {key_type}")

            def fake_ensure(source, destination, amount, *, tron_client=None):
                calls.append((source, destination, amount, tron_client))

            tasks.get_key = fake_get_key
            tasks.ensure_usdt_transfer_resources = fake_ensure
            tasks.get_energy_provider = lambda tron_client=None: self.fail(
                "external USDT sweep must use shared resource provisioning"
            )
            tasks.get_bandwidth_provider = lambda tron_client=None: self.fail(
                "external USDT sweep must use shared resource provisioning"
            )
            tasks.estimate_trc20_sweep_energy = lambda *args, **kwargs: self.fail(
                "external USDT sweep must not use legacy fixed/provider estimate"
            )

            result = tasks.transfer_trc20_from.run(onetime, "USDT")
        finally:
            tasks.config = original_config
            tasks.ConnectionManager = original_connection_manager
            tasks.get_key = original_get_key
            tasks.get_energy_provider = original_get_energy_provider
            tasks.get_bandwidth_provider = original_get_bandwidth_provider
            tasks.ensure_usdt_transfer_resources = original_ensure
            tasks.estimate_trc20_sweep_energy = original_estimate

        self.assertEqual(result["tx_token"], {"receipt": {"result": "SUCCESS"}})
        self.assertEqual(
            calls,
            [(onetime, fee_deposit, Decimal("3"), client)],
        )
        self.assertEqual(client.energy_estimate_calls, 0)

    def test_external_usdt_sweep_activates_source_before_shared_provisioning(self):
        from app import tasks
        from app.schemas import KeyType

        fee_deposit = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
        onetime = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
        client = InactiveSourceSweepTronClient(onetime)
        calls = []
        original_config = tasks.config
        original_connection_manager = tasks.ConnectionManager
        original_get_key = tasks.get_key
        original_ensure = tasks.ensure_usdt_transfer_resources
        original_guard = tasks.fee_deposit_spend_guard_for_address
        try:
            tasks.config = FakeConfig()
            tasks.ConnectionManager = SimpleNamespace(client=lambda: client)

            def fake_get_key(key_type, pub=None):
                if key_type == KeyType.fee_deposit:
                    return object(), fee_deposit
                if key_type == KeyType.onetime:
                    return object(), pub
                raise AssertionError(f"unexpected key type {key_type}")

            def fake_ensure(source, destination, amount, *, tron_client=None):
                calls.append((source, destination, amount, tron_client))

            @contextmanager
            def fake_guard(*_args, **_kwargs):
                client.source_active = True
                yield

            tasks.get_key = fake_get_key
            tasks.ensure_usdt_transfer_resources = fake_ensure
            tasks.fee_deposit_spend_guard_for_address = fake_guard

            result = tasks.transfer_trc20_from.run(onetime, "USDT")
        finally:
            tasks.config = original_config
            tasks.ConnectionManager = original_connection_manager
            tasks.get_key = original_get_key
            tasks.ensure_usdt_transfer_resources = original_ensure
            tasks.fee_deposit_spend_guard_for_address = original_guard

        self.assertEqual(result["tx_trx_res"], {"receipt": {"result": "SUCCESS"}})
        self.assertEqual(result["tx_token"], {"receipt": {"result": "SUCCESS"}})
        self.assertEqual(client.trx.transfers, [(fee_deposit, onetime, 100_000)])
        self.assertEqual(calls, [(onetime, fee_deposit, Decimal("3"), client)])

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

    def test_refee_usdt_sweep_resource_error_returns_before_broadcast(self):
        from app import tasks
        from app.schemas import KeyType
        from app.usdt_resource_provisioning import UsdtResourceError

        fee_deposit = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
        onetime = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
        client = NoBroadcastTronClient()
        original_config = tasks.config
        original_connection_manager = tasks.ConnectionManager
        original_get_key = tasks.get_key
        original_get_energy_provider = tasks.get_energy_provider
        original_get_bandwidth_provider = tasks.get_bandwidth_provider
        original_ensure = tasks.ensure_usdt_transfer_resources
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
            tasks.ensure_usdt_transfer_resources = lambda *args, **kwargs: (_ for _ in ()).throw(
                UsdtResourceError(
                    "provider unavailable",
                    code="PROVIDER_UNAVAILABLE",
                    temporary=True,
                )
            )
            tasks.get_energy_provider = lambda tron_client=None: self.fail(
                "external USDT sweep must not call legacy energy provider on shared failure"
            )
            tasks.get_bandwidth_provider = lambda tron_client=None: self.fail(
                "external USDT sweep must not call legacy bandwidth provider on shared failure"
            )

            result = tasks.transfer_trc20_from.run(onetime, "USDT")
        finally:
            tasks.config = original_config
            tasks.ConnectionManager = original_connection_manager
            tasks.get_key = original_get_key
            tasks.get_energy_provider = original_get_energy_provider
            tasks.get_bandwidth_provider = original_get_bandwidth_provider
            tasks.ensure_usdt_transfer_resources = original_ensure

        self.assertIsNone(result)
        self.assertEqual(client.energy_estimate_calls, 0)

    def test_refee_usdt_sweep_does_not_use_fixed_refee_energy_path(self):
        from app import tasks
        from app.schemas import KeyType

        fee_deposit = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
        onetime = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
        client = SuccessfulSweepTronClient()
        calls = []
        original_config = tasks.config
        original_connection_manager = tasks.ConnectionManager
        original_get_key = tasks.get_key
        original_get_energy_provider = tasks.get_energy_provider
        original_get_bandwidth_provider = tasks.get_bandwidth_provider
        original_ensure = tasks.ensure_usdt_transfer_resources
        original_estimate = tasks.estimate_trc20_sweep_energy
        try:
            tasks.config = FakeConfig()
            tasks.ConnectionManager = SimpleNamespace(client=lambda: client)

            def fake_get_key(key_type, pub=None):
                if key_type == KeyType.fee_deposit:
                    return object(), fee_deposit
                if key_type == KeyType.onetime:
                    return object(), pub
                raise AssertionError(f"unexpected key type {key_type}")

            def fake_ensure(source, destination, amount, *, tron_client=None):
                calls.append((source, destination, amount, tron_client))

            tasks.get_key = fake_get_key
            tasks.ensure_usdt_transfer_resources = fake_ensure
            tasks.estimate_trc20_sweep_energy = lambda *args, **kwargs: self.fail(
                "REFEE_FIXED_ENERGY_ORDER_AMOUNT must not be used for external USDT sweep"
            )
            tasks.get_energy_provider = lambda tron_client=None: self.fail(
                "external USDT sweep must not directly request re:Fee energy provider"
            )
            tasks.get_bandwidth_provider = lambda tron_client=None: self.fail(
                "external USDT sweep must not directly request re:Fee bandwidth provider"
            )

            result = tasks.transfer_trc20_from.run(onetime, "USDT")
        finally:
            tasks.config = original_config
            tasks.ConnectionManager = original_connection_manager
            tasks.get_key = original_get_key
            tasks.get_energy_provider = original_get_energy_provider
            tasks.get_bandwidth_provider = original_get_bandwidth_provider
            tasks.ensure_usdt_transfer_resources = original_ensure
            tasks.estimate_trc20_sweep_energy = original_estimate

        self.assertEqual(result["tx_token"], {"receipt": {"result": "SUCCESS"}})
        self.assertEqual(calls, [(onetime, fee_deposit, Decimal("3"), client)])

    def test_profeex_usdt_sweep_uses_shared_provisioning_not_direct_providers(self):
        from app import tasks
        from app.schemas import KeyType

        fee_deposit = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
        onetime = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
        client = SuccessfulSweepTronClient()
        calls = []
        original_config = tasks.config
        original_connection_manager = tasks.ConnectionManager
        original_get_key = tasks.get_key
        original_get_energy_provider = tasks.get_energy_provider
        original_get_bandwidth_provider = tasks.get_bandwidth_provider
        original_ensure = tasks.ensure_usdt_transfer_resources
        try:
            tasks.config = ProfeeXEnergyConfig()
            tasks.ConnectionManager = SimpleNamespace(client=lambda: client)

            def fake_get_key(key_type, pub=None):
                if key_type == KeyType.fee_deposit:
                    return object(), fee_deposit
                if key_type == KeyType.onetime:
                    return object(), pub
                raise AssertionError(f"unexpected key type {key_type}")

            def fake_ensure(source, destination, amount, *, tron_client=None):
                calls.append((source, destination, amount, tron_client))

            tasks.get_key = fake_get_key
            tasks.ensure_usdt_transfer_resources = fake_ensure
            tasks.get_energy_provider = lambda tron_client=None: self.fail(
                "ProfeeX USDT sweep must use shared resource provisioning"
            )
            tasks.get_bandwidth_provider = lambda tron_client=None: self.fail(
                "ProfeeX USDT sweep must use shared resource provisioning"
            )

            result = tasks.transfer_trc20_from.run(onetime, "USDT")
        finally:
            tasks.config = original_config
            tasks.ConnectionManager = original_connection_manager
            tasks.get_key = original_get_key
            tasks.get_energy_provider = original_get_energy_provider
            tasks.get_bandwidth_provider = original_get_bandwidth_provider
            tasks.ensure_usdt_transfer_resources = original_ensure

        self.assertEqual(result["tx_token"], {"receipt": {"result": "SUCCESS"}})
        self.assertEqual(calls, [(onetime, fee_deposit, Decimal("3"), client)])

    def test_profeex_usdt_resource_failure_does_not_use_refee_burn_fallback(self):
        from app import tasks
        from app.schemas import KeyType
        from app.usdt_resource_provisioning import UsdtResourceError

        fee_deposit = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
        onetime = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
        client = NoBroadcastTronClient()
        original_config = tasks.config
        original_connection_manager = tasks.ConnectionManager
        original_get_key = tasks.get_key
        original_get_energy_provider = tasks.get_energy_provider
        original_get_bandwidth_provider = tasks.get_bandwidth_provider
        original_ensure = tasks.ensure_usdt_transfer_resources
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
            tasks.ensure_usdt_transfer_resources = lambda *args, **kwargs: (_ for _ in ()).throw(
                UsdtResourceError("network unavailable", code="PROVIDER_FAILED", temporary=True)
            )
            tasks.get_energy_provider = lambda tron_client=None: self.fail(
                "ProfeeX USDT sweep must not call legacy provider after shared failure"
            )
            tasks.get_bandwidth_provider = lambda tron_client=None: self.fail(
                "ProfeeX USDT sweep must not call legacy provider after shared failure"
            )
            tasks._fund_onetime_for_trc20_burn = lambda *args, **kwargs: self.fail(
                "ProfeeX shared resource failure must not use TRX burn fallback"
            )

            result = tasks.transfer_trc20_from.run(onetime, "USDT")
        finally:
            tasks.config = original_config
            tasks.ConnectionManager = original_connection_manager
            tasks.get_key = original_get_key
            tasks.get_energy_provider = original_get_energy_provider
            tasks.get_bandwidth_provider = original_get_bandwidth_provider
            tasks.ensure_usdt_transfer_resources = original_ensure
            tasks._fund_onetime_for_trc20_burn = original_fund_onetime

        self.assertIsNone(result)

    def test_external_usdt_sweep_does_not_release_legacy_provider_after_shared_success(self):
        from app import tasks
        from app.schemas import KeyType

        fee_deposit = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
        onetime = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
        client = SuccessfulSweepTronClient()
        original_config = tasks.config
        original_connection_manager = tasks.ConnectionManager
        original_get_key = tasks.get_key
        original_get_energy_provider = tasks.get_energy_provider
        original_get_bandwidth_provider = tasks.get_bandwidth_provider
        original_ensure = tasks.ensure_usdt_transfer_resources
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
            tasks.ensure_usdt_transfer_resources = lambda *args, **kwargs: None
            tasks.get_energy_provider = lambda tron_client=None: self.fail(
                "external USDT sweep must not bind legacy provider for release"
            )
            tasks.get_bandwidth_provider = lambda tron_client=None: self.fail(
                "external USDT sweep must not bind legacy bandwidth provider"
            )

            result = tasks.transfer_trc20_from.run(onetime, "USDT")
        finally:
            tasks.config = original_config
            tasks.ConnectionManager = original_connection_manager
            tasks.get_key = original_get_key
            tasks.get_energy_provider = original_get_energy_provider
            tasks.get_bandwidth_provider = original_get_bandwidth_provider
            tasks.ensure_usdt_transfer_resources = original_ensure

        self.assertEqual(result["tx_token"], {"receipt": {"result": "SUCCESS"}})

    def test_staking_successful_sweep_calls_release_energy(self):
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
        original_get_energy_delegator = tasks.get_energy_delegator
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
            tasks.get_energy_provider = lambda tron_client=None: provider
            tasks.get_bandwidth_provider = lambda tron_client=None: provider
            tasks.get_energy_delegator = lambda: (object(), "TDELEGATOR")

            result = tasks.transfer_trc20_from.run(onetime, "USDT")
        finally:
            tasks.config = original_config
            tasks.ConnectionManager = original_connection_manager
            tasks.get_key = original_get_key
            tasks.get_energy_provider = original_get_energy_provider
            tasks.get_bandwidth_provider = original_get_bandwidth_provider
            tasks.get_energy_delegator = original_get_energy_delegator

        self.assertEqual(result["tx_token"], {"receipt": {"result": "SUCCESS"}})
        self.assertEqual(provider.release_calls, [onetime])

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

    def test_refee_provider_bandwidth_recheck_failure_marks_order_accepted(self):
        from app.resource_providers.refee import RefeeProvider

        class FakeSettings:
            min_bandwidth_order_amount = 1_000
            rent_duration_label = "1h"
            bandwidth_rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01

        class StillInsufficientBandwidthTronClient:
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
                        "NetLimit": 0,
                        "NetUsed": 0,
                    },
                ]

            def get_account_resource(self, _address):
                if len(self.resources) > 1:
                    return self.resources.pop(0)
                return self.resources[0]

        provider = RefeeProvider(tron_client=StillInsufficientBandwidthTronClient())
        provider._create_order = lambda *args, **kwargs: {
            "id": "order-1",
            "status": "pending",
        }
        provider._wait_until_delegated = lambda settings, order_id, order: {
            "id": order_id,
            "status": "delegated",
        }

        module = __import__("app.resource_providers.refee", fromlist=["config"])
        original_config = module.config
        module.config = SimpleNamespace(REFEE=FakeSettings())
        try:
            acquired = provider.acquire_bandwidth(
                "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7",
                346,
            )
        finally:
            module.config = original_config

        self.assertFalse(acquired)
        self.assertEqual(provider.last_failure.code, "RESOURCE_RECHECK_FAILED")
        self.assertTrue(provider.last_failure.temporary)
        self.assertFalse(provider.last_failure.fallback_eligible)
        self.assertTrue(provider.last_failure.order_accepted)
        self.assertEqual(provider.last_failure.task_id, "order-1")

    def test_refee_provider_bandwidth_post_delegation_read_failure_marks_order_accepted(self):
        from app.resource_providers.refee import RefeeProvider

        class FakeSettings:
            min_bandwidth_order_amount = 1_000
            rent_duration_label = "1h"
            bandwidth_rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01

        class ReadFailsAfterDelegationTronClient:
            def __init__(self):
                self.calls = 0

            def get_account_resource(self, _address):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "freeNetLimit": 600,
                        "freeNetUsed": 600,
                        "NetLimit": 0,
                        "NetUsed": 0,
                    }
                raise RuntimeError("resource read failed")

        provider = RefeeProvider(tron_client=ReadFailsAfterDelegationTronClient())
        provider._create_order = lambda *args, **kwargs: {
            "id": "order-1",
            "status": "pending",
        }
        provider._wait_until_delegated = lambda settings, order_id, order: {
            "id": order_id,
            "status": "delegated",
        }

        module = __import__("app.resource_providers.refee", fromlist=["config"])
        original_config = module.config
        module.config = SimpleNamespace(REFEE=FakeSettings())
        try:
            acquired = provider.acquire_bandwidth(
                "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7",
                346,
            )
        finally:
            module.config = original_config

        self.assertFalse(acquired)
        self.assertEqual(provider.last_failure.code, "RESOURCE_READ_FAILED")
        self.assertTrue(provider.last_failure.temporary)
        self.assertFalse(provider.last_failure.fallback_eligible)
        self.assertTrue(provider.last_failure.order_accepted)
        self.assertEqual(provider.last_failure.task_id, "order-1")

    def test_refee_provider_malformed_accepted_bandwidth_order_marks_order_accepted(self):
        from app.resource_providers.refee import RefeeProvider

        class FakeSettings:
            min_bandwidth_order_amount = 1_000
            rent_duration_label = "1h"
            bandwidth_rent_duration_label = "1h"
            timeout_sec = 1
            poll_interval_sec = 0.01
            api_base_url = "https://api.refee.bot"
            api_key = SimpleNamespace(get_secret_value=lambda: "token")

        class InsufficientBandwidthTronClient:
            def get_account_resource(self, _address):
                return {
                    "freeNetLimit": 600,
                    "freeNetUsed": 600,
                    "NetLimit": 0,
                    "NetUsed": 0,
                }

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
                    tron_client=InsufficientBandwidthTronClient()
                )
                module = __import__("app.resource_providers.refee", fromlist=["config"])
                original_config = module.config
                original_requests = module.requests
                module.config = SimpleNamespace(REFEE=FakeSettings())
                module.requests = SimpleNamespace(post=lambda *args, **kwargs: response)
                try:
                    acquired = provider.acquire_bandwidth(
                        "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7",
                        346,
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

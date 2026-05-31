from decimal import Decimal
from types import SimpleNamespace
import unittest


FEE_DEPOSIT = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
DESTINATION = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"


class FakeClient:
    def __init__(self, resources):
        self.resources = list(resources)
        self.resource_calls = []

    def get_account_resource(self, address):
        self.resource_calls.append(address)
        if len(self.resources) > 1:
            return self.resources.pop(0)
        return self.resources[0]


class RecordingEnergyProvider:
    def __init__(self, result=True):
        self.result = result
        self.acquire_calls = []

    def acquire_energy(self, *args, **kwargs):
        self.acquire_calls.append((args, kwargs))
        return self.result

    def release_energy(self, _receiver):
        pass


class RecordingBandwidthProvider:
    def __init__(self, result=True):
        self.result = result
        self.acquire_calls = []

    def acquire_bandwidth(self, *args, **kwargs):
        self.acquire_calls.append((args, kwargs))
        return self.result


class PayoutResourcesTests(unittest.TestCase):
    def patch_module(
        self,
        module,
        *,
        config=None,
        fee_estimate=None,
        energy_provider=None,
        bandwidth_provider=None,
        has_free_bw=True,
    ):
        original_config = module.config
        original_get_key = module.get_key
        original_estimate = module.estimate_usdt_transfer_fee_via_profeex
        original_get_energy_provider = module.get_energy_provider
        original_get_bandwidth_provider = module.get_bandwidth_provider
        original_has_free_bw = module.has_free_bw
        module.config = config or SimpleNamespace(
            ENERGY_PROVIDER="refee",
            BANDWIDTH_PROVIDER="refee",
            BANDWIDTH_PER_TRC20_TRANSFER_CALL=346,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS=1,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_SLEEP_SEC=0,
        )
        module.get_key = lambda _key_type: (object(), FEE_DEPOSIT)
        module.estimate_usdt_transfer_fee_via_profeex = lambda _destination: fee_estimate
        module.get_energy_provider = lambda tron_client=None: energy_provider
        module.get_bandwidth_provider = lambda tron_client=None: bandwidth_provider
        module.has_free_bw = (
            lambda account, required, tron_client=None: has_free_bw
        )

        def restore():
            module.config = original_config
            module.get_key = original_get_key
            module.estimate_usdt_transfer_fee_via_profeex = original_estimate
            module.get_energy_provider = original_get_energy_provider
            module.get_bandwidth_provider = original_get_bandwidth_provider
            module.has_free_bw = original_has_free_bw

        return restore

    def test_quote_uses_profeex_fee_endpoint_for_energy_required(self):
        from app import payout_resources

        client = FakeClient(
            [
                {
                    "EnergyLimit": 10_000,
                    "EnergyUsed": 0,
                    "freeNetLimit": 600,
                    "freeNetUsed": 0,
                    "NetLimit": 0,
                    "NetUsed": 0,
                }
            ]
        )
        restore = self.patch_module(
            payout_resources,
            fee_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": "0",
            },
            energy_provider=object(),
            bandwidth_provider=object(),
        )
        try:
            quote = payout_resources.estimate_fee_deposit_resources_for_usdt_payout(
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertEqual(quote.source_address, FEE_DEPOSIT)
        self.assertEqual(quote.energy.required, 65_000)
        self.assertEqual(quote.energy.available, 10_000)
        self.assertEqual(quote.energy.deficit, 55_000)
        self.assertTrue(quote.submit_ready)

    def test_quote_preserves_profeex_activation_and_trx_burn_fields(self):
        from app import payout_resources

        client = FakeClient(
            [
                {
                    "EnergyLimit": 0,
                    "EnergyUsed": 0,
                    "freeNetLimit": 600,
                    "freeNetUsed": 0,
                    "NetLimit": 0,
                    "NetUsed": 0,
                }
            ]
        )
        restore = self.patch_module(
            payout_resources,
            fee_estimate={
                "energy_required": 65_000,
                "is_new_address": True,
                "trx_burned": "1.1",
            },
            energy_provider=object(),
            bandwidth_provider=object(),
        )
        try:
            quote = payout_resources.estimate_fee_deposit_resources_for_usdt_payout(
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertTrue(quote.activation_required)
        self.assertEqual(quote.estimated_trx_burned, "1.1")
        self.assertFalse(quote.submit_ready)
        self.assertEqual(quote.blocking_code, "DESTINATION_NOT_ACTIVATED")

    def test_quote_blocks_when_profeex_fee_estimate_fails(self):
        from app import payout_resources

        client = FakeClient(
            [
                {
                    "EnergyLimit": 0,
                    "EnergyUsed": 0,
                    "freeNetLimit": 600,
                    "freeNetUsed": 0,
                    "NetLimit": 0,
                    "NetUsed": 0,
                }
            ]
        )
        restore = self.patch_module(
            payout_resources,
            fee_estimate=None,
            energy_provider=object(),
            bandwidth_provider=object(),
        )
        try:
            quote = payout_resources.estimate_fee_deposit_resources_for_usdt_payout(
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertFalse(quote.submit_ready)
        self.assertEqual(quote.blocking_code, "PROFEEX_ESTIMATE_UNAVAILABLE")

    def test_quote_blocks_staking_provider_for_fee_wallet_energy_deficit(self):
        from app import payout_resources

        config = SimpleNamespace(
            ENERGY_PROVIDER="staking",
            BANDWIDTH_PROVIDER="refee",
            BANDWIDTH_PER_TRC20_TRANSFER_CALL=346,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS=1,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_SLEEP_SEC=0,
        )
        client = FakeClient(
            [
                {
                    "EnergyLimit": 0,
                    "EnergyUsed": 0,
                    "freeNetLimit": 600,
                    "freeNetUsed": 0,
                    "NetLimit": 0,
                    "NetUsed": 0,
                }
            ]
        )
        restore = self.patch_module(
            payout_resources,
            config=config,
            fee_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": "0",
            },
            energy_provider=object(),
            bandwidth_provider=object(),
        )
        try:
            quote = payout_resources.estimate_fee_deposit_resources_for_usdt_payout(
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertFalse(quote.submit_ready)
        self.assertEqual(quote.energy.provider, None)
        self.assertEqual(quote.blocking_code, "PROVIDER_UNAVAILABLE")

    def test_ensure_calls_configured_energy_provider_then_rechecks_before_return(self):
        from app import payout_resources

        energy_provider = RecordingEnergyProvider()
        client = FakeClient(
            [
                {
                    "EnergyLimit": 0,
                    "EnergyUsed": 0,
                    "freeNetLimit": 600,
                    "freeNetUsed": 0,
                    "NetLimit": 0,
                    "NetUsed": 0,
                },
                {
                    "EnergyLimit": 0,
                    "EnergyUsed": 0,
                    "freeNetLimit": 600,
                    "freeNetUsed": 0,
                    "NetLimit": 0,
                    "NetUsed": 0,
                },
                {
                    "EnergyLimit": 65_000,
                    "EnergyUsed": 0,
                    "freeNetLimit": 600,
                    "freeNetUsed": 0,
                    "NetLimit": 0,
                    "NetUsed": 0,
                },
            ]
        )
        restore = self.patch_module(
            payout_resources,
            fee_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": "0",
            },
            energy_provider=energy_provider,
            bandwidth_provider=object(),
        )
        try:
            quote = payout_resources.ensure_fee_deposit_resources_for_usdt_payout(
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertEqual(quote.energy.deficit, 0)
        self.assertEqual(len(energy_provider.acquire_calls), 1)
        args, kwargs = energy_provider.acquire_calls[0]
        self.assertEqual(args[0], FEE_DEPOSIT)
        self.assertEqual(args[1], 65_000)
        self.assertEqual(kwargs["minimum_energy_required"], 65_000)

    def test_ensure_calls_configured_bandwidth_provider_then_rechecks_before_return(self):
        from app import payout_resources

        bandwidth_provider = RecordingBandwidthProvider()
        client = FakeClient(
            [
                {
                    "EnergyLimit": 65_000,
                    "EnergyUsed": 0,
                    "freeNetLimit": 600,
                    "freeNetUsed": 600,
                    "NetLimit": 0,
                    "NetUsed": 0,
                },
                {
                    "EnergyLimit": 65_000,
                    "EnergyUsed": 0,
                    "freeNetLimit": 600,
                    "freeNetUsed": 600,
                    "NetLimit": 346,
                    "NetUsed": 0,
                },
            ]
        )
        has_free_bw_values = iter([False, True])
        restore = self.patch_module(
            payout_resources,
            fee_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": "0",
            },
            energy_provider=object(),
            bandwidth_provider=bandwidth_provider,
        )
        original_has_free_bw = payout_resources.has_free_bw
        payout_resources.has_free_bw = (
            lambda account, required, tron_client=None: next(has_free_bw_values)
        )
        try:
            quote = payout_resources.ensure_fee_deposit_resources_for_usdt_payout(
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            payout_resources.has_free_bw = original_has_free_bw
            restore()

        self.assertEqual(quote.bandwidth.deficit, 0)
        self.assertEqual(
            bandwidth_provider.acquire_calls,
            [((FEE_DEPOSIT, 346), {})],
        )

    def test_ensure_raises_before_broadcast_when_resources_still_deficient(self):
        from app import payout_resources

        energy_provider = RecordingEnergyProvider()
        client = FakeClient(
            [
                {
                    "EnergyLimit": 0,
                    "EnergyUsed": 0,
                    "freeNetLimit": 600,
                    "freeNetUsed": 0,
                    "NetLimit": 0,
                    "NetUsed": 0,
                }
            ]
        )
        restore = self.patch_module(
            payout_resources,
            fee_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": "0",
            },
            energy_provider=energy_provider,
            bandwidth_provider=object(),
        )
        try:
            with self.assertRaises(payout_resources.PayoutResourceError) as cm:
                payout_resources.ensure_fee_deposit_resources_for_usdt_payout(
                    DESTINATION,
                    Decimal("1.25"),
                    tron_client=client,
                )
        finally:
            restore()

        self.assertEqual(cm.exception.code, "RESOURCE_RECHECK_FAILED")


if __name__ == "__main__":
    unittest.main()

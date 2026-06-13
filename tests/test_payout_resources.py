from decimal import Decimal
from types import SimpleNamespace
import unittest


FEE_DEPOSIT = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
DESTINATION = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"


class FakeClient:
    def __init__(self, resources, *, destination_active=True):
        self.resources = list(resources)
        self.resource_calls = []
        self.destination_active = destination_active

    def get_account_resource(self, address):
        self.resource_calls.append(address)
        if len(self.resources) > 1:
            return self.resources.pop(0)
        return self.resources[0]

    def get_account(self, _address):
        if self.destination_active is None:
            raise RuntimeError("destination account read failed")
        if self.destination_active:
            return {"address": _address}
        import tronpy.exceptions

        raise tronpy.exceptions.AddressNotFound


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


class RecordingEstimateProvider:
    def __init__(self, result):
        self.result = result
        self.last_failure = None
        self.calls = []

    def estimate_usdt_transfer_fee(self, address):
        self.calls.append(address)
        if callable(self.result):
            return self.result(address)
        return self.result


class PayoutResourcesTests(unittest.TestCase):
    def patch_module(
        self,
        module,
        *,
        config=None,
        fee_estimate=None,
        energy_provider=None,
        fallback_energy_provider=None,
        bandwidth_provider=None,
        fallback_bandwidth_provider=None,
        has_free_bw=True,
    ):
        from app import usdt_resource_provisioning as provisioning

        original_config = module.config
        original_get_key = module.get_key
        original_estimate = getattr(
            module, "estimate_usdt_transfer_fee_via_profeex", None
        )
        original_provisioning_config = provisioning.config
        original_profeex_provider = provisioning.ProfeeXProvider
        original_refee_provider = provisioning.RefeeProvider
        original_get_energy_provider = provisioning.get_energy_provider
        original_get_bandwidth_provider = provisioning.get_bandwidth_provider
        original_get_energy_provider_by_name = provisioning.get_energy_provider_by_name
        original_get_bandwidth_provider_by_name = (
            provisioning.get_bandwidth_provider_by_name
        )
        original_sleep = provisioning.time.sleep
        module.config = config or SimpleNamespace(
            ENERGY_PROVIDER="profeex",
            BANDWIDTH_PROVIDER="profeex",
            TRON_USDT_RESOURCE_FALLBACK_PROVIDER="disabled",
            BANDWIDTH_PER_TRC20_TRANSFER_CALL=346,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS=1,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_SLEEP_SEC=0,
            TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION=False,
        )
        module.get_key = lambda _key_type: (object(), FEE_DEPOSIT)
        provisioning.config = module.config
        profeex_provider = RecordingEstimateProvider(fee_estimate)
        refee_estimate = None
        if getattr(module.config, "TRON_USDT_RESOURCE_FALLBACK_PROVIDER", None) == "refee":
            refee_estimate = {
                "energy_required": 65_000,
                "trx_burned": None,
            }
        refee_provider = RecordingEstimateProvider(refee_estimate)
        provisioning.ProfeeXProvider = lambda: profeex_provider
        provisioning.RefeeProvider = lambda tron_client=None: refee_provider
        provisioning.get_energy_provider = lambda tron_client=None: energy_provider
        provisioning.get_bandwidth_provider = lambda tron_client=None: bandwidth_provider
        provisioning.get_energy_provider_by_name = (
            lambda name, tron_client=None: fallback_energy_provider
        )
        provisioning.get_bandwidth_provider_by_name = (
            lambda name, tron_client=None: fallback_bandwidth_provider
        )
        provisioning.time.sleep = lambda _seconds: None
        if callable(fee_estimate):
            module.estimate_usdt_transfer_fee_via_profeex = fee_estimate
        else:
            module.estimate_usdt_transfer_fee_via_profeex = (
                lambda _destination: fee_estimate
            )

        def restore():
            module.config = original_config
            module.get_key = original_get_key
            if original_estimate is None:
                if hasattr(module, "estimate_usdt_transfer_fee_via_profeex"):
                    delattr(module, "estimate_usdt_transfer_fee_via_profeex")
            else:
                module.estimate_usdt_transfer_fee_via_profeex = original_estimate
            provisioning.config = original_provisioning_config
            provisioning.ProfeeXProvider = original_profeex_provider
            provisioning.RefeeProvider = original_refee_provider
            provisioning.get_energy_provider = original_get_energy_provider
            provisioning.get_bandwidth_provider = original_get_bandwidth_provider
            provisioning.get_energy_provider_by_name = original_get_energy_provider_by_name
            provisioning.get_bandwidth_provider_by_name = (
                original_get_bandwidth_provider_by_name
            )
            provisioning.time.sleep = original_sleep

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
        self.assertEqual(quote.blocking_code, "RESOURCE_ESTIMATE_UNAVAILABLE")

    def test_quote_uses_refee_estimate_when_profeex_estimate_fails_with_refee_fallback(self):
        from app import payout_resources

        config = SimpleNamespace(
            ENERGY_PROVIDER="profeex",
            BANDWIDTH_PROVIDER="profeex",
            TRON_USDT_RESOURCE_FALLBACK_PROVIDER="refee",
            BANDWIDTH_PER_TRC20_TRANSFER_CALL=346,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS=1,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_SLEEP_SEC=0,
            TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION=False,
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
            fee_estimate=None,
            energy_provider=object(),
            fallback_energy_provider=object(),
            bandwidth_provider=object(),
            fallback_bandwidth_provider=object(),
        )
        try:
            quote = payout_resources.estimate_fee_deposit_resources_for_usdt_payout(
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertTrue(quote.submit_ready)
        self.assertEqual(quote.energy.required, 65_000)
        self.assertEqual(quote.energy.deficit, 65_000)
        self.assertEqual(quote.estimate_provider, "refee")
        self.assertFalse(quote.activation_required)
        self.assertIsNone(quote.blocking_code)

    def test_refee_estimate_does_not_skip_destination_activation_check(self):
        from app import payout_resources

        config = SimpleNamespace(
            ENERGY_PROVIDER="profeex",
            BANDWIDTH_PROVIDER="profeex",
            TRON_USDT_RESOURCE_FALLBACK_PROVIDER="refee",
            BANDWIDTH_PER_TRC20_TRANSFER_CALL=346,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS=1,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_SLEEP_SEC=0,
            TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION=False,
        )
        client = FakeClient(
            [
                {
                    "EnergyLimit": 65_000,
                    "EnergyUsed": 0,
                    "freeNetLimit": 600,
                    "freeNetUsed": 0,
                    "NetLimit": 0,
                    "NetUsed": 0,
                }
            ],
            destination_active=False,
        )
        restore = self.patch_module(
            payout_resources,
            config=config,
            fee_estimate=None,
            energy_provider=object(),
            fallback_energy_provider=object(),
            bandwidth_provider=object(),
            fallback_bandwidth_provider=object(),
        )
        try:
            quote = payout_resources.estimate_fee_deposit_resources_for_usdt_payout(
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertEqual(quote.estimate_provider, "refee")
        self.assertTrue(quote.activation_required)
        self.assertFalse(quote.submit_ready)
        self.assertEqual(quote.blocking_code, "DESTINATION_NOT_ACTIVATED")

    def test_quote_blocks_staking_provider_for_fee_wallet_energy_deficit(self):
        from app import payout_resources

        config = SimpleNamespace(
            ENERGY_PROVIDER="staking",
            BANDWIDTH_PROVIDER="refee",
            TRON_USDT_RESOURCE_FALLBACK_PROVIDER="disabled",
            BANDWIDTH_PER_TRC20_TRANSFER_CALL=346,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS=1,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_SLEEP_SEC=0,
            TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION=False,
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

    def test_quote_uses_refee_fallback_when_bandwidth_primary_disabled(self):
        from app import payout_resources

        config = SimpleNamespace(
            ENERGY_PROVIDER="profeex",
            BANDWIDTH_PROVIDER="disabled",
            TRON_USDT_RESOURCE_FALLBACK_PROVIDER="refee",
            BANDWIDTH_PER_TRC20_TRANSFER_CALL=346,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS=1,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_SLEEP_SEC=0,
            TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION=False,
        )
        client = FakeClient(
            [
                {
                    "EnergyLimit": 65_000,
                    "EnergyUsed": 0,
                    "freeNetLimit": 600,
                    "freeNetUsed": 600,
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
            fallback_energy_provider=object(),
            bandwidth_provider=None,
            fallback_bandwidth_provider=object(),
        )
        try:
            quote = payout_resources.estimate_fee_deposit_resources_for_usdt_payout(
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertTrue(quote.submit_ready)
        self.assertEqual(quote.bandwidth.provider, "refee")
        self.assertEqual(quote.bandwidth.deficit, 346)
        self.assertIsNone(quote.blocking_code)

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
        self.assertTrue(kwargs["strict_minimum_required"])

    def test_ensure_falls_back_to_refee_energy_when_primary_provider_fails(self):
        from app import payout_resources

        primary_provider = RecordingEnergyProvider(result=False)
        fallback_provider = RecordingEnergyProvider(result=True)
        config = SimpleNamespace(
            ENERGY_PROVIDER="profeex",
            BANDWIDTH_PROVIDER="disabled",
            TRON_USDT_RESOURCE_FALLBACK_PROVIDER="refee",
            BANDWIDTH_PER_TRC20_TRANSFER_CALL=346,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS=1,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_SLEEP_SEC=0,
            TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION=False,
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
            config=config,
            fee_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": "0",
            },
            energy_provider=primary_provider,
            fallback_energy_provider=fallback_provider,
            bandwidth_provider=None,
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
        self.assertEqual(len(primary_provider.acquire_calls), 1)
        self.assertEqual(len(fallback_provider.acquire_calls), 1)

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
        try:
            quote = payout_resources.ensure_fee_deposit_resources_for_usdt_payout(
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertEqual(quote.bandwidth.deficit, 0)
        self.assertEqual(
            bandwidth_provider.acquire_calls,
            [((FEE_DEPOSIT, 346), {})],
        )

    def test_ensure_falls_back_to_refee_bandwidth_when_primary_provider_fails(self):
        from app import payout_resources

        primary_provider = RecordingBandwidthProvider(result=False)
        fallback_provider = RecordingBandwidthProvider(result=True)
        config = SimpleNamespace(
            ENERGY_PROVIDER="profeex",
            BANDWIDTH_PROVIDER="profeex",
            TRON_USDT_RESOURCE_FALLBACK_PROVIDER="refee",
            BANDWIDTH_PER_TRC20_TRANSFER_CALL=346,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS=1,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_SLEEP_SEC=0,
            TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION=False,
        )
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
                    "NetLimit": 0,
                    "NetUsed": 0,
                },
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
                    "NetLimit": 1_000,
                    "NetUsed": 0,
                },
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
            fallback_energy_provider=object(),
            bandwidth_provider=primary_provider,
            fallback_bandwidth_provider=fallback_provider,
        )
        try:
            quote = payout_resources.ensure_fee_deposit_resources_for_usdt_payout(
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertEqual(quote.bandwidth.deficit, 0)
        self.assertEqual(primary_provider.acquire_calls, [((FEE_DEPOSIT, 346), {})])
        self.assertEqual(fallback_provider.acquire_calls, [((FEE_DEPOSIT, 346), {})])

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

    def test_ensure_raises_when_post_recheck_estimate_is_unavailable(self):
        from app import payout_resources

        energy_provider = RecordingEnergyProvider()
        estimates = iter(
            [
                {
                    "energy_required": 65_000,
                    "is_new_address": False,
                    "trx_burned": "0",
                },
                None,
            ]
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
            fee_estimate=lambda _destination: next(estimates),
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

        self.assertEqual(cm.exception.code, "RESOURCE_ESTIMATE_UNAVAILABLE")

    def test_ensure_activates_destination_when_allowed(self):
        from app import payout_resources
        from app import usdt_resource_provisioning as provisioning

        calls = []
        quote = payout_resources.PayoutResourceQuote(
            source_address=FEE_DEPOSIT,
            destination=DESTINATION,
            amount="1.25",
            activation_required=True,
            estimated_trx_burned="1.1",
            energy=payout_resources.ResourceReadiness("profeex", 65000, 0, 65000),
            bandwidth=payout_resources.ResourceReadiness("profeex", 346, 0, 346),
            submit_ready=False,
            blocking_code="DESTINATION_NOT_ACTIVATED",
            blocking_reason="TRON payout destination is not activated",
        )
        ready_quote = provisioning.UsdtResourceQuote(
            source_address=FEE_DEPOSIT,
            destination=DESTINATION,
            amount="1.25",
            estimate_provider="profeex",
            activation_required=False,
            estimated_trx_burned="6.5",
            energy=provisioning.UsdtResourceReadiness("profeex", 65000, 65000, 0),
            bandwidth=provisioning.UsdtResourceReadiness("profeex", 346, 346, 0),
            submit_ready=True,
            blocking_code=None,
            blocking_reason=None,
        )

        def estimate(destination, amount, tron_client=None):
            return quote

        original_estimate = (
            payout_resources.estimate_fee_deposit_resources_for_usdt_payout
        )
        original_ensure = payout_resources.ensure_usdt_transfer_resources
        original_activation = payout_resources.ensure_destination_activated
        original_flag = (
            payout_resources.config.TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION
        )
        payout_resources.estimate_fee_deposit_resources_for_usdt_payout = estimate
        payout_resources.ensure_usdt_transfer_resources = (
            lambda source, destination, amount, tron_client=None: calls.append(
                ("ensure", source, destination, amount)
            )
            or ready_quote
        )
        payout_resources.ensure_destination_activated = (
            lambda destination, *, quote_fn: calls.append(destination)
        )
        payout_resources.config.TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION = True
        try:
            result = payout_resources.ensure_fee_deposit_resources_for_usdt_payout(
                DESTINATION,
                Decimal("1.25"),
                tron_client=object(),
                allow_destination_activation=True,
            )
        finally:
            payout_resources.estimate_fee_deposit_resources_for_usdt_payout = (
                original_estimate
            )
            payout_resources.ensure_usdt_transfer_resources = original_ensure
            payout_resources.ensure_destination_activated = original_activation
            payout_resources.config.TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION = (
                original_flag
            )

        self.assertEqual(
            calls,
            [DESTINATION, ("ensure", FEE_DEPOSIT, DESTINATION, Decimal("1.25"))],
        )
        self.assertTrue(result.submit_ready)

    def test_ensure_uses_chain_status_quote_for_refee_estimate_activation(self):
        from app import payout_resources
        from app import usdt_resource_provisioning as provisioning

        config = SimpleNamespace(
            ENERGY_PROVIDER="profeex",
            BANDWIDTH_PROVIDER="profeex",
            TRON_USDT_RESOURCE_FALLBACK_PROVIDER="refee",
            BANDWIDTH_PER_TRC20_TRANSFER_CALL=346,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS=1,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_SLEEP_SEC=0,
            TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION=True,
        )
        client = FakeClient(
            [
                {
                    "EnergyLimit": 65_000,
                    "EnergyUsed": 0,
                    "freeNetLimit": 600,
                    "freeNetUsed": 0,
                    "NetLimit": 0,
                    "NetUsed": 0,
                }
            ],
            destination_active=False,
        )
        ready_quote = provisioning.UsdtResourceQuote(
            source_address=FEE_DEPOSIT,
            destination=DESTINATION,
            amount="1.25",
            estimate_provider="refee",
            activation_required=False,
            estimated_trx_burned=None,
            energy=provisioning.UsdtResourceReadiness("refee", 65_000, 65_000, 0),
            bandwidth=provisioning.UsdtResourceReadiness("refee", 346, 346, 0),
            submit_ready=True,
            blocking_code=None,
            blocking_reason=None,
        )
        calls = []
        restore = self.patch_module(
            payout_resources,
            config=config,
            fee_estimate=None,
            energy_provider=object(),
            fallback_energy_provider=object(),
            bandwidth_provider=object(),
            fallback_bandwidth_provider=object(),
        )
        original_activation = payout_resources.ensure_destination_activated
        original_ensure = payout_resources.ensure_usdt_transfer_resources

        def activate(destination, *, quote_fn):
            calls.append(("activate", destination, quote_fn(destination)))

        def ensure(source, destination, amount, tron_client=None):
            calls.append(("ensure", source, destination, amount, tron_client))
            return ready_quote

        payout_resources.ensure_destination_activated = activate
        payout_resources.ensure_usdt_transfer_resources = ensure
        try:
            result = payout_resources.ensure_fee_deposit_resources_for_usdt_payout(
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
                allow_destination_activation=True,
            )
        finally:
            payout_resources.ensure_destination_activated = original_activation
            payout_resources.ensure_usdt_transfer_resources = original_ensure
            restore()

        self.assertEqual(
            calls,
            [
                ("activate", DESTINATION, {"is_new_address": True}),
                ("ensure", FEE_DEPOSIT, DESTINATION, Decimal("1.25"), client),
            ],
        )
        self.assertTrue(result.submit_ready)

    def test_refee_activation_quote_does_not_fallback_to_profeex_when_chain_unknown(self):
        from app import payout_resources

        client = FakeClient([], destination_active=None)
        quote = payout_resources.PayoutResourceQuote(
            source_address=FEE_DEPOSIT,
            destination=DESTINATION,
            amount="1.25",
            activation_required=True,
            estimated_trx_burned=None,
            energy=payout_resources.ResourceReadiness("refee", 65_000, 0, 65_000),
            bandwidth=payout_resources.ResourceReadiness("refee", 346, 0, 346),
            submit_ready=False,
            blocking_code="DESTINATION_NOT_ACTIVATED",
            blocking_reason="TRON payout destination is not activated",
            estimate_provider="refee",
        )
        original_estimate = payout_resources.estimate_usdt_transfer_fee_via_profeex
        payout_resources.estimate_usdt_transfer_fee_via_profeex = (
            lambda _destination: self.fail("ProfeeX quote should not be used")
        )
        try:
            quote_fn = payout_resources._destination_activation_quote_fn(client, quote)
            self.assertIsNone(quote_fn(DESTINATION))
        finally:
            payout_resources.estimate_usdt_transfer_fee_via_profeex = original_estimate

    def test_ensure_preserves_provider_order_accepted_resource_error_metadata(self):
        from app import payout_resources
        from app import usdt_resource_provisioning as provisioning

        quote = payout_resources.PayoutResourceQuote(
            source_address=FEE_DEPOSIT,
            destination=DESTINATION,
            amount="1.25",
            activation_required=False,
            estimated_trx_burned=None,
            energy=payout_resources.ResourceReadiness("profeex", 65_000, 65_000, 0),
            bandwidth=payout_resources.ResourceReadiness("profeex", 346, 346, 0),
            submit_ready=True,
            blocking_code=None,
            blocking_reason=None,
            estimate_provider="profeex",
        )

        def estimate(destination, amount, tron_client=None):
            return quote

        def ensure(source, destination, amount, tron_client=None):
            raise provisioning.UsdtResourceError(
                "Provider order accepted but resources are not visible",
                code="RESOURCE_RECHECK_FAILED",
                temporary=True,
                provider_order_accepted=True,
                provider_task_id="task-1",
            )

        original_estimate = (
            payout_resources.estimate_fee_deposit_resources_for_usdt_payout
        )
        original_ensure = payout_resources.ensure_usdt_transfer_resources
        payout_resources.estimate_fee_deposit_resources_for_usdt_payout = estimate
        payout_resources.ensure_usdt_transfer_resources = ensure
        try:
            with self.assertRaises(payout_resources.PayoutResourceError) as ctx:
                payout_resources.ensure_fee_deposit_resources_for_usdt_payout(
                    DESTINATION,
                    Decimal("1.25"),
                    tron_client=object(),
                )
        finally:
            payout_resources.estimate_fee_deposit_resources_for_usdt_payout = (
                original_estimate
            )
            payout_resources.ensure_usdt_transfer_resources = original_ensure

        self.assertEqual(ctx.exception.code, "RESOURCE_RECHECK_FAILED")
        self.assertTrue(ctx.exception.temporary)
        self.assertTrue(ctx.exception.provider_order_accepted)
        self.assertEqual(ctx.exception.provider_task_id, "task-1")

    def test_ensure_maps_retryable_activation_error_to_resource_error(self):
        from app import payout_resources
        from app.payout_destination_activation import DestinationActivationError

        quote = payout_resources.PayoutResourceQuote(
            source_address=FEE_DEPOSIT,
            destination=DESTINATION,
            amount="1.25",
            activation_required=True,
            estimated_trx_burned="1.1",
            energy=payout_resources.ResourceReadiness("profeex", 65000, 0, 65000),
            bandwidth=payout_resources.ResourceReadiness("profeex", 346, 0, 346),
            submit_ready=False,
            blocking_code="DESTINATION_NOT_ACTIVATED",
            blocking_reason="TRON payout destination is not activated",
        )

        def estimate(destination, amount, tron_client=None):
            return quote

        def activate(destination, *, quote_fn):
            raise DestinationActivationError(
                "ProfeeX activation unavailable",
                code="PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE",
                temporary=True,
            )

        original_estimate = (
            payout_resources.estimate_fee_deposit_resources_for_usdt_payout
        )
        original_activation = payout_resources.ensure_destination_activated
        original_flag = (
            payout_resources.config.TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION
        )
        payout_resources.estimate_fee_deposit_resources_for_usdt_payout = estimate
        payout_resources.ensure_destination_activated = activate
        payout_resources.config.TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION = True
        try:
            with self.assertRaises(payout_resources.PayoutResourceError) as ctx:
                payout_resources.ensure_fee_deposit_resources_for_usdt_payout(
                    DESTINATION,
                    Decimal("1.25"),
                    tron_client=object(),
                    allow_destination_activation=True,
                )
        finally:
            payout_resources.estimate_fee_deposit_resources_for_usdt_payout = (
                original_estimate
            )
            payout_resources.ensure_destination_activated = original_activation
            payout_resources.config.TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION = (
                original_flag
            )

        self.assertEqual(
            ctx.exception.code, "PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE"
        )

    def test_ensure_preserves_terminal_activation_retryability_on_resource_error(self):
        from app import payout_resources
        from app.payout_destination_activation import DestinationActivationError

        quote = payout_resources.PayoutResourceQuote(
            source_address=FEE_DEPOSIT,
            destination=DESTINATION,
            amount="1.25",
            activation_required=True,
            estimated_trx_burned="1.1",
            energy=payout_resources.ResourceReadiness("profeex", 65000, 0, 65000),
            bandwidth=payout_resources.ResourceReadiness("profeex", 346, 0, 346),
            submit_ready=False,
            blocking_code="DESTINATION_NOT_ACTIVATED",
            blocking_reason="TRON payout destination is not activated",
        )

        def estimate(destination, amount, tron_client=None):
            return quote

        def activate(destination, *, quote_fn):
            raise DestinationActivationError(
                "ProfeeX activation unavailable",
                code="PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE",
                temporary=False,
            )

        original_estimate = (
            payout_resources.estimate_fee_deposit_resources_for_usdt_payout
        )
        original_activation = payout_resources.ensure_destination_activated
        original_flag = (
            payout_resources.config.TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION
        )
        payout_resources.estimate_fee_deposit_resources_for_usdt_payout = estimate
        payout_resources.ensure_destination_activated = activate
        payout_resources.config.TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION = True
        try:
            with self.assertRaises(payout_resources.PayoutResourceError) as ctx:
                payout_resources.ensure_fee_deposit_resources_for_usdt_payout(
                    DESTINATION,
                    Decimal("1.25"),
                    tron_client=object(),
                    allow_destination_activation=True,
                )
        finally:
            payout_resources.estimate_fee_deposit_resources_for_usdt_payout = (
                original_estimate
            )
            payout_resources.ensure_destination_activated = original_activation
            payout_resources.config.TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION = (
                original_flag
            )

        self.assertEqual(
            ctx.exception.code, "PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE"
        )
        self.assertFalse(ctx.exception.temporary)


if __name__ == "__main__":
    unittest.main()

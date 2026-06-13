from decimal import Decimal
from types import SimpleNamespace
import unittest


SOURCE = "TSourceAddress"
DESTINATION = "TDestinationAddress"


class FakeClient:
    def __init__(self, resources):
        self.resources = list(resources)
        self.resource_calls = []

    def get_account_resource(self, address):
        self.resource_calls.append(address)
        if len(self.resources) > 1:
            return self.resources.pop(0)
        return self.resources[0]


class FailingAfterFirstReadClient(FakeClient):
    def get_account_resource(self, address):
        self.resource_calls.append(address)
        if len(self.resource_calls) == 1:
            return self.resources[0]
        raise RuntimeError("resource re-read unavailable")


class SequencedThenFailingResourceClient(FakeClient):
    def get_account_resource(self, address):
        self.resource_calls.append(address)
        if self.resources:
            return self.resources.pop(0)
        raise RuntimeError("resource recheck unavailable")


class RecordingEstimateProvider:
    def __init__(self, result, last_failure=None):
        self.result = result
        self.last_failure = last_failure
        self.calls = []

    def estimate_usdt_transfer_fee(self, address):
        self.calls.append(address)
        return self.result


class RecordingEnergyProvider:
    def __init__(self, result, last_failure=None):
        self.result = result
        self.last_failure = last_failure
        self.acquire_calls = []

    def acquire_energy(self, *args, **kwargs):
        self.acquire_calls.append((args, kwargs))
        return self.result

    def release_energy(self, _receiver):
        pass


class RecordingBandwidthProvider:
    def __init__(self, result, last_failure=None):
        self.result = result
        self.last_failure = last_failure
        self.acquire_calls = []

    def acquire_bandwidth(self, *args, **kwargs):
        self.acquire_calls.append((args, kwargs))
        return self.result


def resource(
    *,
    energy_limit=0,
    energy_used=0,
    free_net_limit=0,
    free_net_used=0,
    net_limit=0,
    net_used=0,
):
    return {
        "EnergyLimit": energy_limit,
        "EnergyUsed": energy_used,
        "freeNetLimit": free_net_limit,
        "freeNetUsed": free_net_used,
        "NetLimit": net_limit,
        "NetUsed": net_used,
    }


class UsdtResourceProvisioningTests(unittest.TestCase):
    def patch_module(
        self,
        module,
        *,
        config=None,
        profeex_estimate=None,
        refee_estimate=None,
        energy_provider=None,
        fallback_energy_provider=None,
        bandwidth_provider=None,
        fallback_bandwidth_provider=None,
    ):
        original_config = module.config
        original_profeex_provider = module.ProfeeXProvider
        original_refee_provider = module.RefeeProvider
        original_get_energy_provider = module.get_energy_provider
        original_get_bandwidth_provider = module.get_bandwidth_provider
        original_get_energy_provider_by_name = module.get_energy_provider_by_name
        original_get_bandwidth_provider_by_name = module.get_bandwidth_provider_by_name
        original_sleep = module.time.sleep

        profeex_failure = getattr(profeex_estimate, "last_failure", None)
        profeex_result = getattr(profeex_estimate, "result", profeex_estimate)
        profeex = RecordingEstimateProvider(profeex_result, profeex_failure)
        refee = RecordingEstimateProvider(refee_estimate)
        module.config = config or SimpleNamespace(
            ENERGY_PROVIDER="profeex",
            BANDWIDTH_PROVIDER="profeex",
            TRON_USDT_RESOURCE_FALLBACK_PROVIDER="refee",
            BANDWIDTH_PER_TRC20_TRANSFER_CALL=346,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS=1,
            PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_SLEEP_SEC=0,
        )
        module.ProfeeXProvider = lambda: profeex
        module.RefeeProvider = lambda tron_client=None: refee
        module.get_energy_provider = lambda tron_client=None: energy_provider
        module.get_bandwidth_provider = lambda tron_client=None: bandwidth_provider
        module.get_energy_provider_by_name = (
            lambda name, tron_client=None: fallback_energy_provider
        )
        module.get_bandwidth_provider_by_name = (
            lambda name, tron_client=None: fallback_bandwidth_provider
        )
        module.time.sleep = lambda _seconds: None

        def restore():
            module.config = original_config
            module.ProfeeXProvider = original_profeex_provider
            module.RefeeProvider = original_refee_provider
            module.get_energy_provider = original_get_energy_provider
            module.get_bandwidth_provider = original_get_bandwidth_provider
            module.get_energy_provider_by_name = original_get_energy_provider_by_name
            module.get_bandwidth_provider_by_name = original_get_bandwidth_provider_by_name
            module.time.sleep = original_sleep

        return restore, profeex, refee

    def test_estimate_chain_falls_back_from_profeex_to_refee_with_source_address(self):
        from app import usdt_resource_provisioning as provisioning

        client = FakeClient(
            [
                resource(
                    energy_limit=0,
                    free_net_limit=600,
                    free_net_used=0,
                )
            ]
        )
        restore, profeex, refee = self.patch_module(
            provisioning,
            profeex_estimate=None,
            refee_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": None,
                "provider": "refee",
            },
            energy_provider=object(),
            bandwidth_provider=object(),
        )
        try:
            quote = provisioning.estimate_usdt_transfer_resources(
                SOURCE,
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertEqual(profeex.calls, [DESTINATION])
        self.assertEqual(refee.calls, [SOURCE])
        self.assertEqual(quote.energy.required, 65_000)
        self.assertEqual(quote.estimate_provider, "refee")
        self.assertTrue(quote.submit_ready)

    def test_estimate_chain_uses_refee_primary_without_profeex(self):
        from app import usdt_resource_provisioning as provisioning

        client = FakeClient(
            [
                resource(
                    energy_limit=65_000,
                    free_net_limit=600,
                    free_net_used=0,
                )
            ]
        )
        restore, profeex, refee = self.patch_module(
            provisioning,
            config=SimpleNamespace(
                ENERGY_PROVIDER="refee",
                BANDWIDTH_PROVIDER="refee",
                TRON_USDT_RESOURCE_FALLBACK_PROVIDER="disabled",
                BANDWIDTH_PER_TRC20_TRANSFER_CALL=346,
                PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS=1,
                PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_SLEEP_SEC=0,
            ),
            profeex_estimate={
                "energy_required": 1,
                "is_new_address": False,
                "trx_burned": "0",
            },
            refee_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": None,
                "provider": "refee",
            },
        )
        try:
            quote = provisioning.estimate_usdt_transfer_resources(
                SOURCE,
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertEqual(profeex.calls, [])
        self.assertEqual(refee.calls, [SOURCE])
        self.assertEqual(quote.energy.required, 65_000)
        self.assertEqual(quote.estimate_provider, "refee")
        self.assertTrue(quote.submit_ready)

    def test_estimate_chain_does_not_fall_back_when_profeex_failure_is_not_eligible(self):
        from app import usdt_resource_provisioning as provisioning
        from app.resource_providers.profeex import ProviderFailure

        client = FakeClient([resource(energy_limit=0, free_net_limit=600)])
        restore, profeex, refee = self.patch_module(
            provisioning,
            profeex_estimate=SimpleNamespace(
                result=None,
                last_failure=ProviderFailure(
                    code="AUTHORIZATION_ERROR",
                    temporary=False,
                    fallback_eligible=False,
                ),
            ),
            refee_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": None,
            },
            energy_provider=object(),
            bandwidth_provider=object(),
        )
        try:
            quote = provisioning.estimate_usdt_transfer_resources(
                SOURCE,
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertEqual(profeex.calls, [DESTINATION])
        self.assertEqual(refee.calls, [])
        self.assertFalse(quote.submit_ready)
        self.assertEqual(quote.blocking_code, "AUTHORIZATION_ERROR")

    def test_provision_rents_bandwidth_on_source_only_when_short_primary_then_fallback(self):
        from app import usdt_resource_provisioning as provisioning

        primary = RecordingBandwidthProvider(False)
        fallback = RecordingBandwidthProvider(True)
        client = FakeClient(
            [
                resource(
                    energy_limit=65_000,
                    free_net_limit=600,
                    free_net_used=600,
                ),
                resource(
                    energy_limit=65_000,
                    free_net_limit=600,
                    free_net_used=600,
                ),
                resource(
                    energy_limit=65_000,
                    net_limit=1_000,
                    net_used=0,
                ),
            ]
        )
        restore, _profeex, _refee = self.patch_module(
            provisioning,
            profeex_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": "0",
            },
            bandwidth_provider=primary,
            fallback_bandwidth_provider=fallback,
            energy_provider=object(),
            fallback_energy_provider=object(),
        )
        try:
            quote = provisioning.ensure_usdt_transfer_resources(
                SOURCE,
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertEqual(quote.bandwidth.deficit, 0)
        self.assertEqual(primary.acquire_calls, [((SOURCE, 346), {})])
        self.assertEqual(fallback.acquire_calls, [((SOURCE, 346), {})])
        self.assertTrue(all(address == SOURCE for address in client.resource_calls))

    def test_provision_bandwidth_does_not_fall_back_when_profeex_failure_is_not_eligible(self):
        from app import usdt_resource_provisioning as provisioning
        from app.resource_providers.profeex import ProviderFailure

        primary = RecordingBandwidthProvider(
            False,
            last_failure=ProviderFailure(
                code="ACCEPTED_ORDER_WITHOUT_TASK_ID",
                temporary=True,
                fallback_eligible=False,
                order_accepted=True,
            ),
        )
        fallback = RecordingBandwidthProvider(True)
        client = FakeClient(
            [
                resource(
                    energy_limit=65_000,
                    free_net_limit=600,
                    free_net_used=600,
                )
            ]
        )
        restore, _profeex, _refee = self.patch_module(
            provisioning,
            profeex_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": "0",
            },
            bandwidth_provider=primary,
            fallback_bandwidth_provider=fallback,
            energy_provider=object(),
            fallback_energy_provider=object(),
        )
        try:
            with self.assertRaises(provisioning.UsdtResourceError) as ctx:
                provisioning.ensure_usdt_transfer_resources(
                    SOURCE,
                    DESTINATION,
                    Decimal("1.25"),
                    tron_client=client,
                )
        finally:
            restore()

        self.assertEqual(ctx.exception.code, "ACCEPTED_ORDER_WITHOUT_TASK_ID")
        self.assertTrue(ctx.exception.temporary)
        self.assertTrue(ctx.exception.provider_order_accepted)
        self.assertEqual(primary.acquire_calls, [((SOURCE, 346), {})])
        self.assertEqual(fallback.acquire_calls, [])

    def test_accepted_provider_failure_metadata_is_preserved(self):
        from app import usdt_resource_provisioning as provisioning
        from app.resource_providers.profeex import ProviderFailure

        primary = RecordingBandwidthProvider(
            False,
            last_failure=ProviderFailure(
                code="RESOURCE_RECHECK_FAILED",
                temporary=True,
                fallback_eligible=False,
                order_accepted=True,
                task_id="task-1",
            ),
        )
        client = FakeClient(
            [
                resource(
                    energy_limit=65_000,
                    free_net_limit=600,
                    free_net_used=600,
                )
            ]
        )
        restore, _profeex, _refee = self.patch_module(
            provisioning,
            profeex_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": "0",
            },
            bandwidth_provider=primary,
            energy_provider=object(),
        )
        try:
            with self.assertRaises(provisioning.UsdtResourceError) as ctx:
                provisioning.ensure_usdt_transfer_resources(
                    SOURCE,
                    DESTINATION,
                    Decimal("1.25"),
                    tron_client=client,
                )
        finally:
            restore()

        self.assertEqual(ctx.exception.code, "RESOURCE_RECHECK_FAILED")
        self.assertTrue(ctx.exception.temporary)
        self.assertTrue(ctx.exception.provider_order_accepted)
        self.assertEqual(ctx.exception.provider_task_id, "task-1")

    def test_direct_refee_primary_accepted_failure_metadata_is_preserved(self):
        from app import usdt_resource_provisioning as provisioning

        accepted_failure = SimpleNamespace(
            code="RESOURCE_RECHECK_FAILED",
            temporary=True,
            fallback_eligible=False,
            order_accepted=True,
            task_id="refee-order-1",
        )
        primary = RecordingBandwidthProvider(False, last_failure=accepted_failure)
        client = FakeClient(
            [
                resource(
                    energy_limit=65_000,
                    free_net_limit=600,
                    free_net_used=600,
                )
            ]
        )
        restore, _profeex, _refee = self.patch_module(
            provisioning,
            config=SimpleNamespace(
                ENERGY_PROVIDER="refee",
                BANDWIDTH_PROVIDER="refee",
                TRON_USDT_RESOURCE_FALLBACK_PROVIDER="disabled",
                BANDWIDTH_PER_TRC20_TRANSFER_CALL=346,
                PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS=1,
                PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_SLEEP_SEC=0,
            ),
            refee_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": None,
            },
            bandwidth_provider=primary,
            energy_provider=object(),
        )
        try:
            with self.assertRaises(provisioning.UsdtResourceError) as ctx:
                provisioning.ensure_usdt_transfer_resources(
                    SOURCE,
                    DESTINATION,
                    Decimal("1.25"),
                    tron_client=client,
                )
        finally:
            restore()

        self.assertEqual(ctx.exception.code, "RESOURCE_RECHECK_FAILED")
        self.assertTrue(ctx.exception.provider_order_accepted)
        self.assertEqual(ctx.exception.provider_task_id, "refee-order-1")

    def test_fallback_refee_accepted_failure_metadata_is_preserved(self):
        from app import usdt_resource_provisioning as provisioning

        accepted_failure = SimpleNamespace(
            code="RESOURCE_RECHECK_FAILED",
            temporary=True,
            fallback_eligible=False,
            order_accepted=True,
            task_id="refee-order-2",
        )
        primary = RecordingBandwidthProvider(False)
        fallback = RecordingBandwidthProvider(False, last_failure=accepted_failure)
        client = FakeClient(
            [
                resource(
                    energy_limit=65_000,
                    free_net_limit=600,
                    free_net_used=600,
                ),
                resource(
                    energy_limit=65_000,
                    free_net_limit=600,
                    free_net_used=600,
                ),
            ]
        )
        restore, _profeex, _refee = self.patch_module(
            provisioning,
            profeex_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": "0",
            },
            bandwidth_provider=primary,
            fallback_bandwidth_provider=fallback,
            energy_provider=object(),
            fallback_energy_provider=object(),
        )
        try:
            with self.assertRaises(provisioning.UsdtResourceError) as ctx:
                provisioning.ensure_usdt_transfer_resources(
                    SOURCE,
                    DESTINATION,
                    Decimal("1.25"),
                    tron_client=client,
                )
        finally:
            restore()

        self.assertEqual(ctx.exception.code, "RESOURCE_RECHECK_FAILED")
        self.assertTrue(ctx.exception.provider_order_accepted)
        self.assertEqual(ctx.exception.provider_task_id, "refee-order-2")
        self.assertEqual(primary.acquire_calls, [((SOURCE, 346), {})])
        self.assertEqual(fallback.acquire_calls, [((SOURCE, 346), {})])

    def test_post_provision_resource_read_failure_marks_provider_order_accepted(self):
        from app import usdt_resource_provisioning as provisioning

        primary = RecordingBandwidthProvider(True)
        client = SequencedThenFailingResourceClient(
            [
                resource(
                    energy_limit=65_000,
                    free_net_limit=600,
                    free_net_used=600,
                )
            ]
        )
        restore, _profeex, _refee = self.patch_module(
            provisioning,
            profeex_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": "0",
            },
            bandwidth_provider=primary,
            energy_provider=object(),
        )
        try:
            with self.assertRaises(provisioning.UsdtResourceError) as ctx:
                provisioning.ensure_usdt_transfer_resources(
                    SOURCE,
                    DESTINATION,
                    Decimal("1.25"),
                    tron_client=client,
                )
        finally:
            restore()

        self.assertEqual(ctx.exception.code, "RESOURCE_READ_FAILED")
        self.assertTrue(ctx.exception.temporary)
        self.assertTrue(ctx.exception.provider_order_accepted)

    def test_post_provision_recheck_failure_marks_provider_order_accepted(self):
        from app import usdt_resource_provisioning as provisioning

        primary = RecordingBandwidthProvider(True)
        client = FakeClient(
            [
                resource(
                    energy_limit=65_000,
                    free_net_limit=600,
                    free_net_used=600,
                ),
                resource(
                    energy_limit=65_000,
                    free_net_limit=600,
                    free_net_used=600,
                ),
            ]
        )
        restore, _profeex, _refee = self.patch_module(
            provisioning,
            profeex_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": "0",
            },
            bandwidth_provider=primary,
            energy_provider=object(),
        )
        try:
            with self.assertRaises(provisioning.UsdtResourceError) as ctx:
                provisioning.ensure_usdt_transfer_resources(
                    SOURCE,
                    DESTINATION,
                    Decimal("1.25"),
                    tron_client=client,
                )
        finally:
            restore()

        self.assertEqual(ctx.exception.code, "RESOURCE_RECHECK_FAILED")
        self.assertTrue(ctx.exception.temporary)
        self.assertTrue(ctx.exception.provider_order_accepted)

    def test_provision_skips_fallback_when_primary_failure_made_bandwidth_ready(self):
        from app import usdt_resource_provisioning as provisioning

        primary = RecordingBandwidthProvider(False)
        fallback = RecordingBandwidthProvider(True)
        client = FakeClient(
            [
                resource(
                    energy_limit=65_000,
                    free_net_limit=600,
                    free_net_used=600,
                ),
                resource(
                    energy_limit=65_000,
                    net_limit=1_000,
                    net_used=0,
                ),
                resource(
                    energy_limit=65_000,
                    net_limit=1_000,
                    net_used=0,
                ),
            ]
        )
        restore, _profeex, _refee = self.patch_module(
            provisioning,
            profeex_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": "0",
            },
            bandwidth_provider=primary,
            fallback_bandwidth_provider=fallback,
            energy_provider=object(),
            fallback_energy_provider=object(),
        )
        try:
            quote = provisioning.ensure_usdt_transfer_resources(
                SOURCE,
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertEqual(quote.bandwidth.deficit, 0)
        self.assertEqual(primary.acquire_calls, [((SOURCE, 346), {})])
        self.assertEqual(fallback.acquire_calls, [])

    def test_provision_stops_before_fallback_when_resource_reread_fails(self):
        from app import usdt_resource_provisioning as provisioning

        primary = RecordingBandwidthProvider(False)
        fallback = RecordingBandwidthProvider(True)
        client = FailingAfterFirstReadClient(
            [
                resource(
                    energy_limit=65_000,
                    free_net_limit=600,
                    free_net_used=600,
                )
            ]
        )
        restore, _profeex, _refee = self.patch_module(
            provisioning,
            profeex_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": "0",
            },
            bandwidth_provider=primary,
            fallback_bandwidth_provider=fallback,
            energy_provider=object(),
            fallback_energy_provider=object(),
        )
        try:
            with self.assertRaises(provisioning.UsdtResourceError) as ctx:
                provisioning.ensure_usdt_transfer_resources(
                    SOURCE,
                    DESTINATION,
                    Decimal("1.25"),
                    tron_client=client,
                )
        finally:
            restore()

        self.assertEqual(ctx.exception.code, "RESOURCE_READ_FAILED")
        self.assertTrue(ctx.exception.temporary)
        self.assertEqual(primary.acquire_calls, [((SOURCE, 346), {})])
        self.assertEqual(fallback.acquire_calls, [])
        self.assertTrue(all(address == SOURCE for address in client.resource_calls))

    def test_provision_rents_energy_on_source_only_when_short_with_strict_minimum(self):
        from app import usdt_resource_provisioning as provisioning

        primary = RecordingEnergyProvider(False)
        fallback = RecordingEnergyProvider(True)
        client = FakeClient(
            [
                resource(energy_limit=0, free_net_limit=600),
                resource(energy_limit=0, free_net_limit=600),
                resource(energy_limit=0, free_net_limit=600),
                resource(energy_limit=65_000, free_net_limit=600),
            ]
        )
        restore, _profeex, _refee = self.patch_module(
            provisioning,
            profeex_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": "0",
            },
            energy_provider=primary,
            fallback_energy_provider=fallback,
            bandwidth_provider=object(),
            fallback_bandwidth_provider=object(),
        )
        try:
            quote = provisioning.ensure_usdt_transfer_resources(
                SOURCE,
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertEqual(quote.energy.deficit, 0)
        self.assertEqual(primary.acquire_calls[0][0][0], SOURCE)
        self.assertEqual(fallback.acquire_calls[0][0][0], SOURCE)
        self.assertEqual(
            fallback.acquire_calls[0][1]["minimum_energy_required"],
            65_000,
        )
        self.assertTrue(fallback.acquire_calls[0][1]["strict_minimum_required"])
        self.assertTrue(all(address == SOURCE for address in client.resource_calls))

    def test_provision_returns_temporary_error_when_all_estimates_fail(self):
        from app import usdt_resource_provisioning as provisioning

        client = FakeClient([resource(energy_limit=0, free_net_limit=600)])
        restore, _profeex, _refee = self.patch_module(
            provisioning,
            profeex_estimate=None,
            refee_estimate=None,
            energy_provider=object(),
            bandwidth_provider=object(),
        )
        try:
            with self.assertRaises(provisioning.UsdtResourceError) as cm:
                provisioning.ensure_usdt_transfer_resources(
                    SOURCE,
                    DESTINATION,
                    Decimal("1.25"),
                    tron_client=client,
                )
        finally:
            restore()

        self.assertEqual(cm.exception.code, "RESOURCE_ESTIMATE_UNAVAILABLE")
        self.assertTrue(cm.exception.temporary)

    def test_sufficient_resources_do_not_call_resource_providers(self):
        from app import usdt_resource_provisioning as provisioning

        energy_provider = RecordingEnergyProvider(True)
        bandwidth_provider = RecordingBandwidthProvider(True)
        client = FakeClient(
            [
                resource(
                    energy_limit=65_000,
                    free_net_limit=600,
                    free_net_used=0,
                )
            ]
        )
        restore, _profeex, _refee = self.patch_module(
            provisioning,
            profeex_estimate={
                "energy_required": 65_000,
                "is_new_address": False,
                "trx_burned": "0",
            },
            energy_provider=energy_provider,
            bandwidth_provider=bandwidth_provider,
        )
        provisioning.get_energy_provider = (
            lambda tron_client=None: self.fail("energy provider factory was called")
        )
        provisioning.get_bandwidth_provider = (
            lambda tron_client=None: self.fail("bandwidth provider factory was called")
        )
        provisioning.get_energy_provider_by_name = (
            lambda name, tron_client=None: self.fail(
                "fallback energy provider factory was called"
            )
        )
        provisioning.get_bandwidth_provider_by_name = (
            lambda name, tron_client=None: self.fail(
                "fallback bandwidth provider factory was called"
            )
        )
        try:
            quote = provisioning.ensure_usdt_transfer_resources(
                SOURCE,
                DESTINATION,
                Decimal("1.25"),
                tron_client=client,
            )
        finally:
            restore()

        self.assertTrue(quote.submit_ready)
        self.assertEqual(quote.energy.deficit, 0)
        self.assertEqual(quote.bandwidth.deficit, 0)
        self.assertEqual(energy_provider.acquire_calls, [])
        self.assertEqual(bandwidth_provider.acquire_calls, [])


if __name__ == "__main__":
    unittest.main()

from types import SimpleNamespace
import unittest

from requests import RequestException


class FakeSecret:
    def get_secret_value(self):
        return "profeex-secret"


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


class SequencedBandwidthTronClient:
    def __init__(self, resources):
        self.resources = list(resources)
        self.resource_calls = []

    def get_account_resource(self, address):
        self.resource_calls.append(address)
        if len(self.resources) > 1:
            return self.resources.pop(0)
        return self.resources[0]


class SequencedEnergyTronClient:
    def __init__(self, resources):
        self.resources = list(resources)
        self.resource_calls = []

    def get_account_resource(self, address):
        self.resource_calls.append(address)
        if len(self.resources) > 1:
            return self.resources.pop(0)
        return self.resources[0]


class FailingResourceTronClient:
    def get_account_resource(self, address):
        raise RuntimeError(f"resource unavailable for {address}")


class SequencedResourceOrExceptionTronClient:
    def __init__(self, resources):
        self.resources = list(resources)
        self.resource_calls = []

    def get_account_resource(self, address):
        self.resource_calls.append(address)
        if len(self.resources) > 1:
            resource = self.resources.pop(0)
        else:
            resource = self.resources[0]
        if isinstance(resource, BaseException):
            raise resource
        return resource


class MockJsonResponse:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data
        self.text = str(data)

    def json(self):
        return self._data


class ProfeeXBandwidthProviderTests(unittest.TestCase):
    def patch_config(self, module):
        original_config = module.config
        module.config = SimpleNamespace(PROFEEX=FakeSettings())

        def restore():
            module.config = original_config

        return restore

    def test_profeex_network_timeout_is_fallback_eligible(self):
        from app.resource_providers.profeex import classify_profeex_failure

        failure = classify_profeex_failure("REQUEST_TIMEOUT")

        self.assertEqual(failure.code, "REQUEST_TIMEOUT")
        self.assertTrue(failure.temporary)
        self.assertTrue(failure.fallback_eligible)
        self.assertFalse(failure.order_accepted)

    def test_profeex_insufficient_balance_is_fallback_eligible(self):
        from app.resource_providers.profeex import classify_profeex_failure

        failure = classify_profeex_failure("INSUFFICIENT_BALANCE")

        self.assertEqual(failure.code, "INSUFFICIENT_BALANCE")
        self.assertTrue(failure.temporary)
        self.assertTrue(failure.fallback_eligible)

    def test_profeex_invalid_address_is_not_fallback_eligible(self):
        from app.resource_providers.profeex import classify_profeex_failure

        failure = classify_profeex_failure("INVALID_ADDRESS")

        self.assertEqual(failure.code, "INVALID_ADDRESS")
        self.assertFalse(failure.temporary)
        self.assertFalse(failure.fallback_eligible)

    def test_profeex_malformed_pre_accept_response_is_fallback_eligible(self):
        from app.resource_providers.profeex import classify_profeex_failure

        failure = classify_profeex_failure("MALFORMED_PRE_ACCEPT_RESPONSE")

        self.assertEqual(failure.code, "MALFORMED_PRE_ACCEPT_RESPONSE")
        self.assertTrue(failure.temporary)
        self.assertTrue(failure.fallback_eligible)

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

    def test_profeex_accepted_order_timeout_is_not_fallback_eligible(self):
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

    def test_bandwidth_order_timeout_sets_fallback_eligible_last_failure(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXBandwidthProvider

        client = SequencedBandwidthTronClient(
            [{"freeNetLimit": 0, "freeNetUsed": 0, "NetLimit": 0, "NetUsed": 0}]
        )
        provider = ProfeeXBandwidthProvider(tron_client=client)

        original_post = profeex.requests.post
        restore_config = self.patch_config(profeex)
        try:
            profeex.requests.post = lambda *args, **kwargs: (_ for _ in ()).throw(
                profeex.requests.Timeout("timeout")
            )

            self.assertFalse(provider.acquire_bandwidth("TADDR", 350))
        finally:
            profeex.requests.post = original_post
            restore_config()

        self.assertEqual(provider.last_failure.code, "REQUEST_TIMEOUT")
        self.assertTrue(provider.last_failure.temporary)
        self.assertTrue(provider.last_failure.fallback_eligible)
        self.assertFalse(provider.last_failure.order_accepted)

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
        self.assertEqual(
            client.resource_calls,
            [
                "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7",
                "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7",
            ],
        )

    def test_estimate_usdt_transfer_fee_uses_receiver_address_and_api_key(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXProvider

        gets = []

        def fake_get(url, params, headers, timeout):
            gets.append((url, params, headers, timeout))
            return MockJsonResponse(
                200,
                {
                    "energy_required": 64_300,
                    "is_new_address": False,
                    "trx_burned": "0",
                },
            )

        original_get = profeex.requests.get
        restore_config = self.patch_config(profeex)
        try:
            profeex.requests.get = fake_get
            estimate = ProfeeXProvider().estimate_usdt_transfer_fee(
                "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
            )
        finally:
            profeex.requests.get = original_get
            restore_config()

        self.assertEqual(estimate["energy_required"], 64_300)
        self.assertEqual(
            gets,
            [
                (
                    "https://api.profeex.test/api/v1/delegation/fee",
                    {"receiver_address": "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"},
                    {"X-API-Key": "profeex-secret"},
                    10,
                )
            ],
        )

    def test_estimate_usdt_transfer_fee_rejects_response_without_energy_required(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXProvider

        original_get = profeex.requests.get
        restore_config = self.patch_config(profeex)
        try:
            profeex.requests.get = lambda *args, **kwargs: MockJsonResponse(
                200,
                {"is_new_address": False, "trx_burned": "0"},
            )
            estimate = ProfeeXProvider().estimate_usdt_transfer_fee(
                "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
            )
        finally:
            profeex.requests.get = original_get
            restore_config()

        self.assertIsNone(estimate)

    def test_estimate_usdt_transfer_fee_rejects_negative_energy_required(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXProvider

        original_get = profeex.requests.get
        restore_config = self.patch_config(profeex)
        try:
            profeex.requests.get = lambda *args, **kwargs: MockJsonResponse(
                200,
                {
                    "energy_required": -1,
                    "is_new_address": False,
                    "trx_burned": "0",
                },
            )
            estimate = ProfeeXProvider().estimate_usdt_transfer_fee(
                "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
            )
        finally:
            profeex.requests.get = original_get
            restore_config()

        self.assertIsNone(estimate)

    def test_strict_minimum_required_uses_estimate_as_energy_threshold(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXProvider

        client = SequencedEnergyTronClient(
            [
                {"EnergyLimit": 64_500, "EnergyUsed": 0},
                {"EnergyLimit": 65_000, "EnergyUsed": 0},
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
            self.assertTrue(
                provider.acquire_energy(
                    "TADDR",
                    821,
                    {"EnergyLimit": 64_500, "EnergyUsed": 0},
                    minimum_energy_required=65_000,
                    strict_minimum_required=True,
                )
            )
        finally:
            profeex.requests.post = original_post
            restore_config()

        self.assertEqual(len(posts), 1)

    def test_order_error_marks_duplicate_request_temporary(self):
        from app.resource_providers.profeex import ProfeeXProvider

        error = ProfeeXProvider()._order_error_from_order(
            "energy",
            {
                "error_code": "DUPLICATE_REQUEST",
                "details": {"error_message": "delegation cooldown"},
            },
        )

        self.assertEqual(error.error_code, "DUPLICATE_REQUEST")
        self.assertTrue(error.temporary)
        self.assertIn("delegation cooldown", str(error))

    def test_order_error_marks_rate_limit_temporary(self):
        from app.resource_providers.profeex import ProfeeXProvider

        error = ProfeeXProvider()._order_error_from_order(
            "energy",
            {"error_code": "RATE_LIMIT_EXCEEDED"},
        )

        self.assertEqual(error.error_code, "RATE_LIMIT_EXCEEDED")
        self.assertTrue(error.temporary)

    def test_skips_energy_order_when_fixed_threshold_is_already_available(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXProvider

        client = SequencedEnergyTronClient([{"EnergyLimit": 64_500, "EnergyUsed": 0}])
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

    def test_energy_pre_order_resource_read_failure_sets_non_fallback_last_failure(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXProvider

        provider = ProfeeXProvider(tron_client=FailingResourceTronClient())
        original_post = profeex.requests.post
        restore_config = self.patch_config(profeex)
        try:
            profeex.requests.post = lambda *args, **kwargs: self.fail(
                "post not expected"
            )
            self.assertFalse(
                provider.acquire_energy(
                    "TADDR",
                    7_821,
                    {"EnergyLimit": 0, "EnergyUsed": 0},
                    minimum_energy_required=72_321,
                )
            )
        finally:
            profeex.requests.post = original_post
            restore_config()

        self.assertIsNotNone(provider.last_failure)
        self.assertEqual(provider.last_failure.code, "RESOURCE_READ_FAILED")
        self.assertTrue(provider.last_failure.temporary)
        self.assertFalse(provider.last_failure.fallback_eligible)
        self.assertFalse(provider.last_failure.order_accepted)
        self.assertIsNone(provider.last_failure.task_id)

    def test_energy_post_active_resource_read_failure_stays_non_fallback(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXProvider

        client = SequencedResourceOrExceptionTronClient(
            [
                {"EnergyLimit": 0, "EnergyUsed": 0},
                RuntimeError("post-active resource unavailable"),
                RuntimeError("post-active resource unavailable"),
                RuntimeError("post-active resource unavailable"),
            ]
        )
        provider = ProfeeXProvider(tron_client=client)

        original_post = profeex.requests.post
        restore_config = self.patch_config(profeex)
        try:
            profeex.requests.post = lambda *args, **kwargs: MockJsonResponse(
                202, {"task_id": "task-1", "status": "ACTIVE"}
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
            restore_config()

        self.assertEqual(provider.last_failure.code, "RESOURCE_READ_FAILED")
        self.assertTrue(provider.last_failure.temporary)
        self.assertFalse(provider.last_failure.fallback_eligible)
        self.assertTrue(provider.last_failure.order_accepted)
        self.assertEqual(provider.last_failure.task_id, "task-1")

    def test_energy_recheck_waits_after_active_until_resource_visible(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXProvider

        client = SequencedEnergyTronClient(
            [
                {"EnergyLimit": 0, "EnergyUsed": 0},
                {"EnergyLimit": 0, "EnergyUsed": 0},
                {"EnergyLimit": 65_000, "EnergyUsed": 0},
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
            self.assertTrue(
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

        self.assertEqual(client.resource_calls, ["TADDR", "TADDR", "TADDR"])

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

        self.assertEqual(client.resource_calls, ["TADDR", "TADDR", "TADDR", "TADDR"])

    def test_release_energy_is_noop(self):
        from app.resource_providers.profeex import ProfeeXProvider

        provider = ProfeeXProvider()
        self.assertIsNone(provider.release_energy("TADDR"))

    def test_rents_minimum_bandwidth_with_query_params_and_api_key(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXBandwidthProvider

        client = SequencedBandwidthTronClient(
            [
                {"freeNetLimit": 600, "freeNetUsed": 600, "NetLimit": 0, "NetUsed": 0},
                {"freeNetLimit": 600, "freeNetUsed": 600, "NetLimit": 350, "NetUsed": 0},
            ]
        )
        provider = ProfeeXBandwidthProvider(tron_client=client)
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
            acquired = provider.acquire_bandwidth(
                "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7",
                346,
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
                    "https://api.profeex.test/api/v1/delegation/buybandwidth",
                    {
                        "target": "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7",
                        "volume": 350,
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
        self.assertGreater(gets[0][2], 0)
        self.assertLessEqual(gets[0][2], 10)

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

    def test_active_status_requires_post_delegation_bandwidth_recheck(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXBandwidthProvider

        client = SequencedBandwidthTronClient(
            [
                {"freeNetLimit": 600, "freeNetUsed": 600, "NetLimit": 0, "NetUsed": 0},
                {"freeNetLimit": 600, "freeNetUsed": 600, "NetLimit": 0, "NetUsed": 0},
            ]
        )
        provider = ProfeeXBandwidthProvider(tron_client=client)

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
            self.assertFalse(provider.acquire_bandwidth("TADDR", 350))
        finally:
            profeex.requests.post = original_post
            profeex.requests.get = original_get
            restore_config()

        self.assertEqual(client.resource_calls, ["TADDR", "TADDR", "TADDR", "TADDR"])

    def test_bandwidth_recheck_waits_after_active_until_resource_visible(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXBandwidthProvider

        client = SequencedBandwidthTronClient(
            [
                {"freeNetLimit": 600, "freeNetUsed": 600, "NetLimit": 0, "NetUsed": 0},
                {"freeNetLimit": 600, "freeNetUsed": 600, "NetLimit": 0, "NetUsed": 0},
                {"freeNetLimit": 600, "freeNetUsed": 600, "NetLimit": 350, "NetUsed": 0},
            ]
        )
        provider = ProfeeXBandwidthProvider(tron_client=client)

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
            self.assertTrue(provider.acquire_bandwidth("TADDR", 350))
        finally:
            profeex.requests.post = original_post
            profeex.requests.get = original_get
            restore_config()

        self.assertEqual(client.resource_calls, ["TADDR", "TADDR", "TADDR"])

    def test_bandwidth_post_active_resource_read_failure_stays_non_fallback(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXBandwidthProvider

        client = SequencedResourceOrExceptionTronClient(
            [
                {"freeNetLimit": 600, "freeNetUsed": 600, "NetLimit": 0, "NetUsed": 0},
                RuntimeError("post-active resource unavailable"),
                RuntimeError("post-active resource unavailable"),
                RuntimeError("post-active resource unavailable"),
            ]
        )
        provider = ProfeeXBandwidthProvider(tron_client=client)

        original_post = profeex.requests.post
        restore_config = self.patch_config(profeex)
        try:
            profeex.requests.post = lambda *args, **kwargs: MockJsonResponse(
                202, {"task_id": "task-1", "status": "ACTIVE"}
            )
            self.assertFalse(provider.acquire_bandwidth("TADDR", 350))
        finally:
            profeex.requests.post = original_post
            restore_config()

        self.assertEqual(provider.last_failure.code, "RESOURCE_READ_FAILED")
        self.assertTrue(provider.last_failure.temporary)
        self.assertFalse(provider.last_failure.fallback_eligible)
        self.assertTrue(provider.last_failure.order_accepted)
        self.assertEqual(provider.last_failure.task_id, "task-1")

    def test_minimal_create_response_polls_status_before_rechecking_bandwidth(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXBandwidthProvider

        client = SequencedBandwidthTronClient(
            [
                {"freeNetLimit": 600, "freeNetUsed": 600, "NetLimit": 0, "NetUsed": 0},
                {"freeNetLimit": 600, "freeNetUsed": 600, "NetLimit": 350, "NetUsed": 0},
            ]
        )
        provider = ProfeeXBandwidthProvider(tron_client=client)
        posts = []
        gets = []

        def fake_post(url, params, headers, timeout):
            posts.append((url, params, headers, timeout))
            return MockJsonResponse(202, {"task_id": "task-1"})

        def fake_get(url, headers, timeout):
            gets.append((url, headers, timeout))
            return MockJsonResponse(200, {"task_id": "task-1", "status": "ACTIVE"})

        original_post = profeex.requests.post
        original_get = profeex.requests.get
        restore_config = self.patch_config(profeex)
        try:
            profeex.requests.post = fake_post
            profeex.requests.get = fake_get
            acquired = provider.acquire_bandwidth("TADDR", 350)
        finally:
            profeex.requests.post = original_post
            profeex.requests.get = original_get
            restore_config()

        self.assertTrue(acquired)
        self.assertEqual(len(posts), 1)
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
        self.assertEqual(client.resource_calls, ["TADDR", "TADDR"])

    def test_invalid_status_returns_false_without_polling(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXBandwidthProvider

        provider = ProfeeXBandwidthProvider()
        original_get = profeex.requests.get
        try:
            profeex.requests.get = lambda *args, **kwargs: self.fail(
                "poll not expected"
            )
            invalid_orders = [
                {"task_id": "missing-status"},
                {"task_id": "non-string-status", "status": 123},
                {"task_id": "unexpected-status", "status": "UNEXPECTED"},
            ]
            for order in invalid_orders:
                with self.subTest(order=order):
                    self.assertIsNone(
                        provider._wait_until_active(
                            FakeSettings(), "task-1", order, "bandwidth"
                        )
                    )
        finally:
            profeex.requests.get = original_get

    def test_pending_status_polls_before_sleeping(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXBandwidthProvider

        provider = ProfeeXBandwidthProvider()
        events = []

        def fake_get(*args, **kwargs):
            events.append("get")
            return MockJsonResponse(200, {"task_id": "task-1", "status": "ACTIVE"})

        def fake_sleep(_seconds):
            events.append("sleep")

        original_get = profeex.requests.get
        original_sleep = profeex.time.sleep
        try:
            profeex.requests.get = fake_get
            profeex.time.sleep = fake_sleep
            self.assertEqual(
                provider._wait_until_active(
                    FakeSettings(),
                    "task-1",
                    {"task_id": "task-1", "status": "QUEUED"},
                    "bandwidth",
                ),
                {"task_id": "task-1", "status": "ACTIVE"},
            )
        finally:
            profeex.requests.get = original_get
            profeex.time.sleep = original_sleep

        self.assertEqual(events, ["get"])

    def test_skips_order_when_bandwidth_is_already_available(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXBandwidthProvider

        client = SequencedBandwidthTronClient(
            [{"freeNetLimit": 600, "freeNetUsed": 0, "NetLimit": 0, "NetUsed": 0}]
        )
        provider = ProfeeXBandwidthProvider(tron_client=client)
        original_post = profeex.requests.post
        restore_config = self.patch_config(profeex)
        try:
            profeex.requests.post = lambda *args, **kwargs: self.fail(
                "post not expected"
            )
            self.assertTrue(provider.acquire_bandwidth("TADDR", 346))
        finally:
            profeex.requests.post = original_post
            restore_config()

    def test_fixed_bandwidth_below_large_required_fails_before_order(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXBandwidthProvider

        client = SequencedBandwidthTronClient(
            [{"freeNetLimit": 0, "freeNetUsed": 0, "NetLimit": 0, "NetUsed": 0}]
        )
        provider = ProfeeXBandwidthProvider(tron_client=client)
        original_post = profeex.requests.post
        restore_config = self.patch_config(profeex)
        try:
            profeex.requests.post = lambda *args, **kwargs: self.fail(
                "post not expected"
            )
            self.assertFalse(provider.acquire_bandwidth("TADDR", 10_001))
        finally:
            profeex.requests.post = original_post
            restore_config()

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

    def test_failed_status_returns_false(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXBandwidthProvider

        client = SequencedBandwidthTronClient(
            [{"freeNetLimit": 0, "freeNetUsed": 0, "NetLimit": 0, "NetUsed": 0}]
        )
        provider = ProfeeXBandwidthProvider(tron_client=client)

        original_post = profeex.requests.post
        original_get = profeex.requests.get
        restore_config = self.patch_config(profeex)
        try:
            profeex.requests.post = lambda *args, **kwargs: MockJsonResponse(
                202, {"task_id": "task-1", "status": "QUEUED"}
            )
            profeex.requests.get = lambda *args, **kwargs: MockJsonResponse(
                200,
                {
                    "task_id": "task-1",
                    "status": "FAILED",
                    "error_code": "INSUFFICIENT_BALANCE",
                    "details": {"error_message": "not enough balance"},
                },
            )
            self.assertFalse(provider.acquire_bandwidth("TADDR", 350))
        finally:
            profeex.requests.post = original_post
            profeex.requests.get = original_get
            restore_config()

    def test_timeout_returns_false_after_transient_poll_failures(self):
        from app.resource_providers import profeex
        from app.resource_providers.profeex import ProfeeXBandwidthProvider

        client = SequencedBandwidthTronClient(
            [{"freeNetLimit": 0, "freeNetUsed": 0, "NetLimit": 0, "NetUsed": 0}]
        )
        provider = ProfeeXBandwidthProvider(tron_client=client)

        original_post = profeex.requests.post
        original_get = profeex.requests.get
        restore_config = self.patch_config(profeex)
        try:
            profeex.requests.post = lambda *args, **kwargs: MockJsonResponse(
                202, {"task_id": "task-1", "status": "QUEUED"}
            )
            profeex.requests.get = lambda *args, **kwargs: (_ for _ in ()).throw(
                RequestException("temporary")
            )
            self.assertFalse(provider.acquire_bandwidth("TADDR", 350))
        finally:
            profeex.requests.post = original_post
            profeex.requests.get = original_get
            restore_config()


if __name__ == "__main__":
    unittest.main()

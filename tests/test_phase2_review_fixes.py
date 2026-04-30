import ast
import inspect
import unittest

from pydantic import ValidationError
from requests import RequestException

from app import energy_provider
from app.refee import RefeeConfig
from app.utils import has_free_bw


class FakeTronClient:
    def __init__(self):
        self.resource_calls = []

    def get_account_resource(self, account):
        self.resource_calls.append(account)
        return {
            "freeNetLimit": 1000,
            "freeNetUsed": 0,
            "NetLimit": 0,
            "NetUsed": 0,
        }


class FakeSecret:
    def get_secret_value(self):
        return "secret"


class RefeeSettings:
    api_base_url = "https://api.refee.test"
    api_key = FakeSecret()
    timeout_sec = 0.01
    poll_interval_sec = 0


class Phase2ReviewFixTests(unittest.TestCase):
    def test_has_free_bw_uses_selected_tron_client_when_provided(self):
        client = FakeTronClient()

        self.assertTrue(has_free_bw("TADDR", 100, tron_client=client))
        self.assertEqual(client.resource_calls, ["TADDR"])

    def test_transfer_trc20_from_passes_selected_tron_client_to_bandwidth_checks(self):
        from app import tasks

        source = inspect.getsource(tasks.transfer_trc20_from)
        tree = ast.parse(source)

        has_free_bw_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "has_free_bw"
        ]

        self.assertGreaterEqual(len(has_free_bw_calls), 3)
        for call in has_free_bw_calls:
            self.assertTrue(
                any(
                    kw.arg == "tron_client"
                    and isinstance(kw.value, ast.Name)
                    and kw.value.id == "tron_client"
                    for kw in call.keywords
                )
            )

    def test_refee_config_rejects_empty_api_key(self):
        with self.assertRaises(ValidationError):
            RefeeConfig(api_key="")

    def test_refee_provider_uses_success_status_set(self):
        provider = energy_provider.RefeeEnergyProvider()
        provider.SUCCESS_STATUSES = {"custom-success"}

        original_get = energy_provider.requests.get
        energy_provider.requests.get = MockRequestGet()
        try:
            order = provider._wait_until_delegated(
                RefeeSettings(), "order-1", {"id": "order-1", "status": "custom-success"}
            )
        finally:
            energy_provider.requests.get = original_get

        self.assertEqual(order, {"id": "order-1", "status": "custom-success"})


class MockRequestGet:
    def __call__(self, *args, **kwargs):
        raise RequestException("poll should not run for configured success status")


if __name__ == "__main__":
    unittest.main()

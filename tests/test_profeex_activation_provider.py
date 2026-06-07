from types import SimpleNamespace
import unittest

from app.resource_providers import profeex


class FakeSettings:
    api_base_url = "https://api.profeex.test/api/v1"
    api_key = SimpleNamespace(get_secret_value=lambda: "secret")
    currency = "TRX"
    timeout_sec = 1
    poll_interval_sec = 0.01


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class ProfeeXActivationProviderTests(unittest.TestCase):
    def setUp(self):
        self.original_config = profeex.config
        self.original_post = profeex.requests.post
        self.original_get = profeex.requests.get
        profeex.config = SimpleNamespace(PROFEEX=FakeSettings())

    def tearDown(self):
        profeex.config = self.original_config
        profeex.requests.post = self.original_post
        profeex.requests.get = self.original_get

    def test_activate_address_posts_expected_query_params(self):
        captured = {}

        def post(url, params, headers, timeout):
            captured.update(
                {
                    "url": url,
                    "params": params,
                    "headers": headers,
                    "timeout": timeout,
                }
            )
            return FakeResponse(
                202,
                {
                    "task_id": "task-1",
                    "target": "TTMqzSAwwcM1UqMy7Up2eQuNXZ6uUZ9AN5",
                    "status": "QUEUED",
                },
            )

        profeex.requests.post = post

        result = profeex.ProfeeXProvider().activate_address(
            "TTMqzSAwwcM1UqMy7Up2eQuNXZ6uUZ9AN5"
        )

        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(
            captured["url"],
            "https://api.profeex.test/api/v1/activation/activate",
        )
        self.assertEqual(
            captured["params"],
            {
                "address": "TTMqzSAwwcM1UqMy7Up2eQuNXZ6uUZ9AN5",
                "currency": "TRX",
            },
        )
        self.assertEqual(captured["headers"], {"X-API-Key": "secret"})

    def test_activation_409_is_retryable_duplicate(self):
        profeex.requests.post = lambda *args, **kwargs: FakeResponse(
            409,
            {"message": "duplicate request"},
        )

        with self.assertRaises(profeex.ProfeeXOrderError) as ctx:
            profeex.ProfeeXProvider().activate_address(
                "TTMqzSAwwcM1UqMy7Up2eQuNXZ6uUZ9AN5"
            )

        self.assertEqual(ctx.exception.resource_name, "activation")
        self.assertEqual(ctx.exception.error_code, "DUPLICATE_REQUEST")
        self.assertTrue(ctx.exception.temporary)

    def test_activation_503_is_retryable_unavailable(self):
        profeex.requests.post = lambda *args, **kwargs: FakeResponse(
            503,
            {"message": "service unavailable"},
        )

        with self.assertRaises(profeex.ProfeeXOrderError) as ctx:
            profeex.ProfeeXProvider().activate_address(
                "TTMqzSAwwcM1UqMy7Up2eQuNXZ6uUZ9AN5"
            )

        self.assertEqual(ctx.exception.resource_name, "activation")
        self.assertEqual(ctx.exception.error_code, "SERVICE_UNAVAILABLE")
        self.assertTrue(ctx.exception.temporary)

    def test_activation_request_exception_is_retryable_unavailable(self):
        def post(*args, **kwargs):
            raise profeex.requests.RequestException("timeout")

        profeex.requests.post = post

        with self.assertRaises(profeex.ProfeeXOrderError) as ctx:
            profeex.ProfeeXProvider().activate_address(
                "TTMqzSAwwcM1UqMy7Up2eQuNXZ6uUZ9AN5"
            )

        self.assertEqual(ctx.exception.resource_name, "activation")
        self.assertEqual(ctx.exception.error_code, "SERVICE_UNAVAILABLE")
        self.assertTrue(ctx.exception.temporary)

    def test_activation_422_invalid_address_is_terminal(self):
        profeex.requests.post = lambda *args, **kwargs: FakeResponse(
            422,
            {"error_code": "INVALID_ADDRESS", "message": "invalid address"},
        )

        with self.assertRaises(profeex.ProfeeXOrderError) as ctx:
            profeex.ProfeeXProvider().activate_address("not-a-tron-address")

        self.assertEqual(ctx.exception.resource_name, "activation")
        self.assertEqual(ctx.exception.error_code, "INVALID_ADDRESS")
        self.assertFalse(ctx.exception.temporary)

    def test_wait_for_activation_treats_completed_as_success(self):
        polls = iter(
            [
                FakeResponse(200, {"task_id": "task-1", "status": "PROCESSING"}),
                FakeResponse(200, {"task_id": "task-1", "status": "COMPLETED"}),
            ]
        )
        profeex.requests.get = lambda *args, **kwargs: next(polls)

        result = profeex.ProfeeXProvider().wait_for_activation(
            FakeSettings(),
            "task-1",
            {"task_id": "task-1", "status": "QUEUED"},
        )

        self.assertEqual(result["status"], "COMPLETED")

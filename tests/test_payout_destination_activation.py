from decimal import Decimal
from types import SimpleNamespace
import json
import unittest

import prometheus_client

from app import payout_destination_activation as activation
from app.resource_providers.profeex import ProviderFailure, ProfeeXOrderError
from app.resource_providers.refee import RefeeProvider, RefeeProviderError


DESTINATION = "TTMqzSAwwcM1UqMy7Up2eQuNXZ6uUZ9AN5"


class FakeLock:
    def __init__(self, events):
        self.events = events

    def acquire(self, blocking=True):
        self.events.append(("lock_acquire", blocking))
        return True

    def release(self):
        self.events.append(("lock_release",))


class RejectingLock(FakeLock):
    def acquire(self, blocking=True):
        self.events.append(("lock_acquire", blocking))
        return False


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.events = []

    def lock(self, name, timeout, blocking_timeout, thread_local):
        self.events.append(("lock", name, timeout, blocking_timeout, thread_local))
        return FakeLock(self.events)

    def get(self, key):
        self.events.append(("get", key))
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self.events.append(("setex", key, ttl, json.loads(value)))
        self.values[key] = value.encode("utf-8") if isinstance(value, str) else value

    def delete(self, key):
        self.events.append(("delete", key))
        self.values.pop(key, None)


class RejectingLockRedis(FakeRedis):
    def lock(self, name, timeout, blocking_timeout, thread_local):
        self.events.append(("lock", name, timeout, blocking_timeout, thread_local))
        return RejectingLock(self.events)


class RedisGetFailingRedis(FakeRedis):
    def get(self, key):
        self.events.append(("get", key))
        raise activation.redis.exceptions.RedisError("redis get unavailable")


class RedisSetFailingRedis(FakeRedis):
    def setex(self, key, ttl, value):
        self.events.append(("setex", key, ttl, json.loads(value)))
        raise activation.redis.exceptions.RedisError("redis set unavailable")


class RedisSecondSetFailingRedis(FakeRedis):
    def __init__(self):
        super().__init__()
        self.setex_calls = 0

    def setex(self, key, ttl, value):
        self.setex_calls += 1
        self.events.append(("setex", key, ttl, json.loads(value)))
        if self.setex_calls > 1:
            raise activation.redis.exceptions.RedisError("redis set unavailable")
        self.values[key] = value.encode("utf-8") if isinstance(value, str) else value


class FakeProvider:
    def __init__(self):
        self.calls = []

    def activate_address(self, destination):
        self.calls.append(("activate", destination))
        return {"task_id": "task-1", "target": destination, "status": "QUEUED"}

    def wait_for_activation(self, settings, task_id, order):
        self.calls.append(("wait", task_id, order["status"]))
        return {"task_id": task_id, "target": DESTINATION, "status": "COMPLETED"}


class FailingProvider(FakeProvider):
    def wait_for_activation(self, settings, task_id, order):
        self.calls.append(("wait", task_id, order["status"]))
        raise ProfeeXOrderError(
            "activation",
            "provider unavailable",
            "SERVICE_UNAVAILABLE",
            temporary=True,
        )


class DuplicateProvider(FakeProvider):
    def activate_address(self, destination):
        self.calls.append(("activate", destination))
        raise ProfeeXOrderError(
            "activation",
            "duplicate request",
            "DUPLICATE_REQUEST",
            temporary=True,
        )


class TimeoutProvider(FakeProvider):
    def wait_for_activation(self, settings, task_id, order):
        self.calls.append(("wait", task_id, order["status"]))
        raise ProfeeXOrderError(
            "activation",
            "provider timeout",
            "REQUEST_TIMEOUT",
            temporary=True,
        )


class MalformedOrderProvider(FakeProvider):
    def activate_address(self, destination):
        self.calls.append(("activate", destination))
        return {"target": destination, "status": "QUEUED"}


class ActivationFailingProvider(FakeProvider):
    def __init__(self, temporary=True):
        super().__init__()
        self.temporary = temporary

    def activate_address(self, destination):
        self.calls.append(("activate", destination))
        raise ProfeeXOrderError(
            "activation",
            "provider unavailable",
            "SERVICE_UNAVAILABLE",
            temporary=self.temporary,
        )


class AcceptedWithoutTaskProvider(FakeProvider):
    def activate_address(self, destination):
        self.calls.append(("activate", destination))
        raise ProfeeXOrderError(
            "activation",
            "accepted activation response had no task_id",
            "ACCEPTED_ORDER_WITHOUT_TASK_ID",
            temporary=True,
            provider_failure=ProviderFailure(
                code="ACCEPTED_ORDER_WITHOUT_TASK_ID",
                temporary=True,
                fallback_eligible=False,
                order_accepted=True,
            ),
        )


class DestinationActivationFailingProvider(FakeProvider):
    def __init__(self, temporary=True):
        super().__init__()
        self.temporary = temporary

    def activate_address(self, destination):
        self.calls.append(("activate", destination))
        raise activation.DestinationActivationError(
            "activation unavailable",
            code="PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE",
            temporary=self.temporary,
        )


class FakeRefeeActivationProvider:
    def __init__(self, result=None, error=None):
        self.result = result or {"txn_hash": "tx-refee-1"}
        self.error = error
        self.calls = []

    def activate_address(self, destination):
        self.calls.append(("activate", destination))
        if self.error is not None:
            raise self.error
        return self.result


class FakeResponse:
    def __init__(self, status_code, payload=None, text="response"):
        self.status_code = status_code
        self.payload = payload
        self.text = text

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class DestinationActivationTests(unittest.TestCase):
    def setUp(self):
        self.original_config = activation.config
        activation.config = SimpleNamespace(
            REDIS_HOST="localhost",
            PROFEEX=SimpleNamespace(timeout_sec=1, poll_interval_sec=0.01),
            REFEE=SimpleNamespace(
                api_base_url="https://refee.test",
                api_key=SimpleNamespace(get_secret_value=lambda: "refee-key"),
            ),
            TRON_USDT_RESOURCE_FALLBACK_PROVIDER=None,
            TRON_USDT_DESTINATION_ACTIVATION_LOCK_TTL_SEC=300,
            TRON_USDT_DESTINATION_ACTIVATION_LOCK_WAIT_SEC=60,
            TRON_USDT_DESTINATION_ACTIVATION_RECORD_TTL_SEC=86400,
        )
        from app.payout_observability import clear_destination_activation_metrics

        clear_destination_activation_metrics()

    def tearDown(self):
        activation.config = self.original_config
        from app.payout_observability import clear_destination_activation_metrics

        clear_destination_activation_metrics()

    def quote_sequence(self, *is_new_values):
        values = list(is_new_values)

        def quote(destination):
            return {
                "is_new_address": values.pop(0),
                "energy_required": 65000,
                "trx_burned": Decimal("1.1"),
            }

        return quote

    def test_activates_once_and_persists_task_id(self):
        redis_client = FakeRedis()
        provider = FakeProvider()

        result = activation.ensure_destination_activated(
            DESTINATION,
            quote_fn=self.quote_sequence(True, True, False),
            provider=provider,
            redis_client=redis_client,
        )

        self.assertTrue(result.activated)
        self.assertEqual(
            provider.calls, [("activate", DESTINATION), ("wait", "task-1", "QUEUED")]
        )
        self.assertTrue(
            any(
                event[0] == "setex" and event[3]["task_id"] == "task-1"
                for event in redis_client.events
            )
        )
        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_destination_activation_total{result="success"} 1.0',
            text,
        )
        self.assertIn(
            "tron_payout_destination_activation_duration_seconds_count 1.0",
            text,
        )

    def test_active_destination_skip_records_metric_without_lock_or_provider(self):
        redis_client = FakeRedis()
        provider = FakeProvider()

        result = activation.ensure_destination_activated(
            DESTINATION,
            quote_fn=self.quote_sequence(False),
            provider=provider,
            redis_client=redis_client,
        )

        self.assertFalse(result.activated)
        self.assertEqual(provider.calls, [])
        self.assertFalse(any(event[0] == "lock" for event in redis_client.events))
        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_destination_activation_total{result="success"} 1.0',
            text,
        )
        self.assertIn(
            "tron_payout_destination_activation_duration_seconds_count 1.0",
            text,
        )

    def test_unacquired_lock_is_not_released(self):
        redis_client = RejectingLockRedis()
        provider = FakeProvider()

        with self.assertRaises(activation.DestinationActivationError) as ctx:
            activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=self.quote_sequence(True),
                provider=provider,
                redis_client=redis_client,
            )

        self.assertEqual(ctx.exception.code, "PAYOUT_DESTINATION_ACTIVATION_PENDING")
        self.assertEqual(provider.calls, [])
        self.assertTrue(any(event[0] == "lock_acquire" for event in redis_client.events))
        self.assertFalse(any(event[0] == "lock_release" for event in redis_client.events))

    def test_unknown_quote_raises_before_provider_and_records_retryable_metric(self):
        from app.payout_observability import clear_destination_activation_metrics

        for quote in (None, {"energy_required": 65000}):
            with self.subTest(quote=quote):
                clear_destination_activation_metrics()
                redis_client = FakeRedis()
                provider = FakeProvider()

                with self.assertRaises(activation.DestinationActivationError) as ctx:
                    activation.ensure_destination_activated(
                        DESTINATION,
                        quote_fn=lambda destination: quote,
                        provider=provider,
                        redis_client=redis_client,
                    )

                self.assertEqual(
                    ctx.exception.code,
                    "PAYOUT_DESTINATION_ACTIVATION_QUOTE_UNAVAILABLE",
                )
                self.assertTrue(ctx.exception.temporary)
                self.assertEqual(provider.calls, [])
                self.assertFalse(any(event[0] == "lock" for event in redis_client.events))
                text = prometheus_client.generate_latest().decode()
                self.assertIn(
                    'tron_payout_destination_activation_total{result="retryable_error"} 1.0',
                    text,
                )
                self.assertNotIn(
                    'tron_payout_destination_activation_total{result="success"} 1.0',
                    text,
                )

    def test_quote_exception_raises_before_provider(self):
        redis_client = FakeRedis()
        provider = FakeProvider()

        def quote(destination):
            raise RuntimeError("quote unavailable")

        with self.assertRaises(activation.DestinationActivationError) as ctx:
            activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=quote,
                provider=provider,
                redis_client=redis_client,
            )

        self.assertEqual(
            ctx.exception.code, "PAYOUT_DESTINATION_ACTIVATION_QUOTE_UNAVAILABLE"
        )
        self.assertTrue(ctx.exception.temporary)
        self.assertEqual(provider.calls, [])
        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_destination_activation_total{result="retryable_error"} 1.0',
            text,
        )

    def test_redis_get_failure_raises_before_provider(self):
        redis_client = RedisGetFailingRedis()
        provider = FakeProvider()

        with self.assertRaises(activation.DestinationActivationError) as ctx:
            activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=self.quote_sequence(True, True),
                provider=provider,
                redis_client=redis_client,
            )

        self.assertEqual(
            ctx.exception.code, "PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE"
        )
        self.assertTrue(ctx.exception.temporary)
        self.assertEqual(provider.calls, [])
        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_destination_activation_total{result="retryable_error"} 1.0',
            text,
        )

    def test_redis_set_failure_after_activation_records_retryable_error(self):
        redis_client = RedisSetFailingRedis()
        provider = FakeProvider()

        with self.assertRaises(activation.DestinationActivationError) as ctx:
            activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=self.quote_sequence(True, True),
                provider=provider,
                redis_client=redis_client,
            )

        self.assertEqual(
            ctx.exception.code, "PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE"
        )
        self.assertTrue(ctx.exception.temporary)
        self.assertEqual(provider.calls, [("activate", DESTINATION)])
        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_destination_activation_total{result="retryable_error"} 1.0',
            text,
        )
        self.assertNotIn(
            'tron_payout_destination_activation_total{result="success"} 1.0',
            text,
        )

    def test_wait_error_mapping_survives_failure_record_store_error(self):
        redis_client = RedisSecondSetFailingRedis()
        provider = TimeoutProvider()

        with self.assertRaises(activation.DestinationActivationError) as ctx:
            activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=self.quote_sequence(True, True),
                provider=provider,
                redis_client=redis_client,
            )

        self.assertEqual(ctx.exception.code, "PAYOUT_DESTINATION_ACTIVATION_TIMEOUT")
        self.assertTrue(ctx.exception.temporary)
        self.assertEqual(
            provider.calls, [("activate", DESTINATION), ("wait", "task-1", "QUEUED")]
        )

    def test_failed_record_replays_terminal_error_without_provider_call(self):
        redis_client = FakeRedis()
        redis_client.values[activation.activation_record_key(DESTINATION)] = json.dumps(
            {
                "destination": DESTINATION,
                "task_id": "task-failed",
                "status": "FAILED",
                "error_code": "PROCESSING_FAILED",
            }
        ).encode("utf-8")
        provider = FakeProvider()

        with self.assertRaises(activation.DestinationActivationError) as ctx:
            activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=self.quote_sequence(True, True),
                provider=provider,
                redis_client=redis_client,
            )

        self.assertEqual(ctx.exception.code, "PROCESSING_FAILED")
        self.assertFalse(ctx.exception.temporary)
        self.assertEqual(provider.calls, [])
        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_destination_activation_total{result="terminal_error"} 1.0',
            text,
        )

    def test_unknown_record_replays_terminal_error_without_provider_call(self):
        redis_client = FakeRedis()
        redis_client.values[activation.activation_record_key(DESTINATION)] = json.dumps(
            {
                "destination": DESTINATION,
                "task_id": "task-unknown",
                "status": "unknown",
                "error_code": "UNKNOWN_ERROR",
                "error_message": "provider returned unknown status",
            }
        ).encode("utf-8")
        provider = FakeProvider()

        with self.assertRaises(activation.DestinationActivationError) as ctx:
            activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=self.quote_sequence(True, True),
                provider=provider,
                redis_client=redis_client,
            )

        self.assertEqual(ctx.exception.code, "UNKNOWN_ERROR")
        self.assertFalse(ctx.exception.temporary)
        self.assertEqual(provider.calls, [])
        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_destination_activation_total{result="terminal_error"} 1.0',
            text,
        )

    def test_completed_record_with_active_quote_returns_success_without_provider_call(self):
        redis_client = FakeRedis()
        redis_client.values[activation.activation_record_key(DESTINATION)] = json.dumps(
            {
                "destination": DESTINATION,
                "task_id": "task-completed",
                "status": "COMPLETED",
            }
        ).encode("utf-8")
        provider = FakeProvider()

        result = activation.ensure_destination_activated(
            DESTINATION,
            quote_fn=self.quote_sequence(True, True, False),
            provider=provider,
            redis_client=redis_client,
        )

        self.assertTrue(result.activated)
        self.assertEqual(result.task_id, "task-completed")
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(provider.calls, [])
        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_destination_activation_total{result="success"} 1.0',
            text,
        )

    def test_missing_profeex_config_on_pending_record_raises_before_wait(self):
        activation.config.PROFEEX = None
        redis_client = FakeRedis()
        redis_client.values[activation.activation_record_key(DESTINATION)] = json.dumps(
            {
                "destination": DESTINATION,
                "task_id": "task-existing",
                "status": "PROCESSING",
            }
        ).encode("utf-8")
        provider = FakeProvider()

        with self.assertRaises(activation.DestinationActivationError) as ctx:
            activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=self.quote_sequence(True, True),
                provider=provider,
                redis_client=redis_client,
            )

        self.assertEqual(ctx.exception.code, "CONFIGURATION_ERROR")
        self.assertFalse(ctx.exception.temporary)
        self.assertEqual(provider.calls, [])

    def test_malformed_activation_order_raises_terminal_invalid_response(self):
        redis_client = FakeRedis()
        provider = MalformedOrderProvider()

        with self.assertRaises(activation.DestinationActivationError) as ctx:
            activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=self.quote_sequence(True, True),
                provider=provider,
                redis_client=redis_client,
            )

        self.assertEqual(
            ctx.exception.code, "PAYOUT_DESTINATION_ACTIVATION_INVALID_RESPONSE"
        )
        self.assertFalse(ctx.exception.temporary)
        self.assertEqual(provider.calls, [("activate", DESTINATION)])

    def test_existing_task_record_is_resumed(self):
        redis_client = FakeRedis()
        redis_client.values[activation.activation_record_key(DESTINATION)] = json.dumps(
            {
                "destination": DESTINATION,
                "task_id": "task-existing",
                "status": "PROCESSING",
            }
        ).encode("utf-8")
        provider = FakeProvider()

        result = activation.ensure_destination_activated(
            DESTINATION,
            quote_fn=self.quote_sequence(True, True, False),
            provider=provider,
            redis_client=redis_client,
        )

        self.assertTrue(result.activated)
        self.assertEqual(provider.calls, [("wait", "task-existing", "PROCESSING")])

    def test_duplicate_activation_rechecks_destination_before_retrying(self):
        redis_client = FakeRedis()
        provider = DuplicateProvider()

        result = activation.ensure_destination_activated(
            DESTINATION,
            quote_fn=self.quote_sequence(True, True, False),
            provider=provider,
            redis_client=redis_client,
        )

        self.assertFalse(result.activated)
        self.assertEqual(result.status, "ALREADY_ACTIVE")
        self.assertEqual(provider.calls, [("activate", DESTINATION)])

    def test_duplicate_activation_does_not_fall_back_when_destination_still_new(self):
        redis_client = FakeRedis()
        profeex_provider = DuplicateProvider()
        refee_provider = FakeRefeeActivationProvider({"txn_hash": "tx-refee-blocked"})

        with self.assertRaises(activation.DestinationActivationError) as ctx:
            activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=self.quote_sequence(True, True, True),
                activation_providers=[
                    ("profeex", profeex_provider),
                    ("refee", refee_provider),
                ],
                redis_client=redis_client,
            )

        self.assertEqual(
            ctx.exception.code, "PAYOUT_DESTINATION_ACTIVATION_DUPLICATE"
        )
        self.assertTrue(ctx.exception.temporary)
        self.assertEqual(profeex_provider.calls, [("activate", DESTINATION)])
        self.assertEqual(refee_provider.calls, [])

    def test_retryable_provider_failure_records_metric(self):
        redis_client = FakeRedis()
        provider = FailingProvider()

        with self.assertRaises(activation.DestinationActivationError) as ctx:
            activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=self.quote_sequence(True, True),
                provider=provider,
                redis_client=redis_client,
            )

        self.assertTrue(ctx.exception.temporary)
        self.assertEqual(
            ctx.exception.code, "PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE"
        )
        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_destination_activation_total{result="retryable_error"} 1.0',
            text,
        )

    def test_temporary_wait_error_after_task_id_does_not_fall_back_to_refee(self):
        redis_client = FakeRedis()
        profeex_provider = FailingProvider()
        refee_provider = FakeRefeeActivationProvider({"txn_hash": "tx-refee-live"})

        with self.assertRaises(activation.DestinationActivationError) as ctx:
            activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=self.quote_sequence(True, True),
                activation_providers=[
                    ("profeex", profeex_provider),
                    ("refee", refee_provider),
                ],
                redis_client=redis_client,
            )

        self.assertTrue(ctx.exception.temporary)
        self.assertEqual(
            ctx.exception.code, "PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE"
        )
        self.assertEqual(
            profeex_provider.calls,
            [("activate", DESTINATION), ("wait", "task-1", "QUEUED")],
        )
        self.assertEqual(refee_provider.calls, [])

    def test_pending_record_wait_error_does_not_fall_back_to_refee(self):
        redis_client = FakeRedis()
        redis_client.values[activation.activation_record_key(DESTINATION)] = json.dumps(
            {
                "destination": DESTINATION,
                "task_id": "task-existing",
                "status": "PROCESSING",
            }
        ).encode("utf-8")
        profeex_provider = FailingProvider()
        refee_provider = FakeRefeeActivationProvider({"txn_hash": "tx-refee-resume"})

        with self.assertRaises(activation.DestinationActivationError) as ctx:
            activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=self.quote_sequence(True, True),
                activation_providers=[
                    ("profeex", profeex_provider),
                    ("refee", refee_provider),
                ],
                redis_client=redis_client,
            )

        self.assertTrue(ctx.exception.temporary)
        self.assertEqual(
            ctx.exception.code, "PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE"
        )
        self.assertEqual(
            profeex_provider.calls, [("wait", "task-existing", "PROCESSING")]
        )
        self.assertEqual(refee_provider.calls, [])

    def test_default_provider_chain_uses_refee_fallback_config(self):
        redis_client = FakeRedis()
        profeex_provider = ActivationFailingProvider(temporary=True)
        refee_provider = FakeRefeeActivationProvider({"txn_hash": "tx-refee-config"})
        original_profeex_provider = activation.ProfeeXProvider
        original_refee_provider = activation.RefeeProvider
        activation.config.TRON_USDT_RESOURCE_FALLBACK_PROVIDER = "refee"
        activation.ProfeeXProvider = lambda: profeex_provider
        activation.RefeeProvider = lambda: refee_provider
        try:
            result = activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=self.quote_sequence(True, True),
                redis_client=redis_client,
            )
        finally:
            activation.ProfeeXProvider = original_profeex_provider
            activation.RefeeProvider = original_refee_provider

        self.assertTrue(result.activated)
        self.assertEqual(result.provider, "refee")
        self.assertEqual(result.txn_hash, "tx-refee-config")
        self.assertEqual(profeex_provider.calls, [("activate", DESTINATION)])
        self.assertEqual(refee_provider.calls, [("activate", DESTINATION)])

    def test_refee_activate_address_posts_destination_and_returns_txn_hash(self):
        import app.resource_providers.refee as refee_module

        original_config = refee_module.config
        original_post = refee_module.requests.post
        calls = []

        def fake_post(url, params=None, headers=None, timeout=None):
            calls.append((url, params, headers, timeout))
            return FakeResponse(200, {"txn_hash": "tx-123", "status": "sent"})

        refee_module.config = activation.config
        refee_module.requests.post = fake_post
        try:
            result = RefeeProvider().activate_address(DESTINATION)
        finally:
            refee_module.config = original_config
            refee_module.requests.post = original_post

        self.assertEqual(result["txn_hash"], "tx-123")
        self.assertEqual(
            calls,
            [
                (
                    "https://refee.test/api/functions/activate",
                    {"address": DESTINATION},
                    {"X-API-Key": "refee-key"},
                    RefeeProvider.REQUEST_TIMEOUT_SEC,
                )
            ],
        )

    def test_refee_activate_address_400_returns_already_active(self):
        import app.resource_providers.refee as refee_module

        original_config = refee_module.config
        original_post = refee_module.requests.post
        refee_module.config = activation.config
        refee_module.requests.post = lambda *args, **kwargs: FakeResponse(400)
        try:
            result = RefeeProvider().activate_address(DESTINATION)
        finally:
            refee_module.config = original_config
            refee_module.requests.post = original_post

        self.assertEqual(
            result, {"status": "already_active", "address": DESTINATION}
        )

    def test_refee_activate_address_402_is_temporary_insufficient_balance(self):
        import app.resource_providers.refee as refee_module

        original_config = refee_module.config
        original_post = refee_module.requests.post
        refee_module.config = activation.config
        refee_module.requests.post = lambda *args, **kwargs: FakeResponse(402)
        try:
            with self.assertRaises(RefeeProviderError) as ctx:
                RefeeProvider().activate_address(DESTINATION)
        finally:
            refee_module.config = original_config
            refee_module.requests.post = original_post

        self.assertEqual(ctx.exception.error_code, "INSUFFICIENT_BALANCE")
        self.assertTrue(ctx.exception.temporary)

    def test_refee_activate_address_401_is_terminal_configuration_error(self):
        import app.resource_providers.refee as refee_module

        original_config = refee_module.config
        original_post = refee_module.requests.post
        refee_module.config = activation.config
        refee_module.requests.post = lambda *args, **kwargs: FakeResponse(401)
        try:
            with self.assertRaises(RefeeProviderError) as ctx:
                RefeeProvider().activate_address(DESTINATION)
        finally:
            refee_module.config = original_config
            refee_module.requests.post = original_post

        self.assertEqual(ctx.exception.error_code, "CONFIGURATION_ERROR")
        self.assertFalse(ctx.exception.temporary)

    def test_temporary_profeex_activation_error_falls_back_to_refee(self):
        redis_client = FakeRedis()
        profeex_provider = ActivationFailingProvider(temporary=True)
        refee_provider = FakeRefeeActivationProvider({"txn_hash": "tx-refee-1"})

        result = activation.ensure_destination_activated(
            DESTINATION,
            quote_fn=self.quote_sequence(True, True),
            activation_providers=[
                ("profeex", profeex_provider),
                ("refee", refee_provider),
            ],
            redis_client=redis_client,
        )

        self.assertTrue(result.activated)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.provider, "refee")
        self.assertEqual(result.txn_hash, "tx-refee-1")
        self.assertEqual(profeex_provider.calls, [("activate", DESTINATION)])
        self.assertEqual(refee_provider.calls, [("activate", DESTINATION)])
        self.assertTrue(
            any(
                event[0] == "setex"
                and event[3]["provider"] == "refee"
                and event[3]["txn_hash"] == "tx-refee-1"
                and event[3]["status"] == "COMPLETED"
                for event in redis_client.events
            )
        )

    def test_accepted_profeex_activation_without_task_id_does_not_fall_back_to_refee(self):
        redis_client = FakeRedis()
        profeex_provider = AcceptedWithoutTaskProvider()
        refee_provider = FakeRefeeActivationProvider({"txn_hash": "tx-refee-blocked"})

        with self.assertRaises(activation.DestinationActivationError) as ctx:
            activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=self.quote_sequence(True, True),
                activation_providers=[
                    ("profeex", profeex_provider),
                    ("refee", refee_provider),
                ],
                redis_client=redis_client,
            )

        self.assertEqual(ctx.exception.code, "ACCEPTED_ORDER_WITHOUT_TASK_ID")
        self.assertTrue(ctx.exception.temporary)
        self.assertEqual(profeex_provider.calls, [("activate", DESTINATION)])
        self.assertEqual(refee_provider.calls, [])

    def test_temporary_destination_activation_error_falls_back_to_refee(self):
        redis_client = FakeRedis()
        profeex_provider = DestinationActivationFailingProvider(temporary=True)
        refee_provider = FakeRefeeActivationProvider({"txn_hash": "tx-refee-2"})

        result = activation.ensure_destination_activated(
            DESTINATION,
            quote_fn=self.quote_sequence(True, True),
            activation_providers=[
                ("profeex", profeex_provider),
                ("refee", refee_provider),
            ],
            redis_client=redis_client,
        )

        self.assertTrue(result.activated)
        self.assertEqual(result.provider, "refee")
        self.assertEqual(result.txn_hash, "tx-refee-2")
        self.assertEqual(profeex_provider.calls, [("activate", DESTINATION)])
        self.assertEqual(refee_provider.calls, [("activate", DESTINATION)])

    def test_terminal_profeex_activation_error_does_not_fall_back_to_refee(self):
        redis_client = FakeRedis()
        profeex_provider = DestinationActivationFailingProvider(temporary=False)
        refee_provider = FakeRefeeActivationProvider({"txn_hash": "tx-refee-3"})

        with self.assertRaises(activation.DestinationActivationError) as ctx:
            activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=self.quote_sequence(True, True),
                activation_providers=[
                    ("profeex", profeex_provider),
                    ("refee", refee_provider),
                ],
                redis_client=redis_client,
            )

        self.assertFalse(ctx.exception.temporary)
        self.assertEqual(profeex_provider.calls, [("activate", DESTINATION)])
        self.assertEqual(refee_provider.calls, [])

    def test_both_activation_providers_fail_temporarily_raises_retryable(self):
        redis_client = FakeRedis()
        profeex_provider = ActivationFailingProvider(temporary=True)
        refee_provider = FakeRefeeActivationProvider(
            error=RefeeProviderError(
                "activation",
                "re:Fee unavailable",
                "SERVICE_UNAVAILABLE",
                temporary=True,
            )
        )

        with self.assertRaises(activation.DestinationActivationError) as ctx:
            activation.ensure_destination_activated(
                DESTINATION,
                quote_fn=self.quote_sequence(True, True),
                activation_providers=[
                    ("profeex", profeex_provider),
                    ("refee", refee_provider),
                ],
                redis_client=redis_client,
            )

        self.assertTrue(ctx.exception.temporary)
        self.assertEqual(
            ctx.exception.code, "PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE"
        )
        self.assertEqual(profeex_provider.calls, [("activate", DESTINATION)])
        self.assertEqual(refee_provider.calls, [("activate", DESTINATION)])
        self.assertFalse(
            any(
                event[0] == "setex" and event[3].get("status") == "COMPLETED"
                for event in redis_client.events
            )
        )

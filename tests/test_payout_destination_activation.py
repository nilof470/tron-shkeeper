from decimal import Decimal
from types import SimpleNamespace
import json
import unittest

import prometheus_client

from app import payout_destination_activation as activation
from app.resource_providers.profeex import ProfeeXOrderError


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


class DestinationActivationTests(unittest.TestCase):
    def setUp(self):
        self.original_config = activation.config
        activation.config = SimpleNamespace(
            REDIS_HOST="localhost",
            PROFEEX=SimpleNamespace(timeout_sec=1, poll_interval_sec=0.01),
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

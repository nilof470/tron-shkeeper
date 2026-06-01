from types import SimpleNamespace
import unittest


class FakeInspect:
    def __init__(self, responses=None, exc=None):
        self.responses = responses
        self.exc = exc

    def active_queues(self):
        if self.exc:
            raise self.exc
        return self.responses


class FakeControl:
    def __init__(self, responses=None, exc=None):
        self.responses = responses
        self.exc = exc
        self.timeout = None

    def inspect(self, timeout=None):
        self.timeout = timeout
        return FakeInspect(self.responses, self.exc)


class CeleryReadinessTests(unittest.TestCase):
    def test_queue_has_consumer_when_worker_reports_queue(self):
        from app.celery_readiness import queue_has_consumer

        control = FakeControl(
            {
                "tron-usdt-payouts@pod": [
                    {"name": "tron_usdt_fee_payouts"},
                ],
            }
        )
        celery_app = SimpleNamespace(control=control)

        self.assertTrue(
            queue_has_consumer(
                "tron_usdt_fee_payouts",
                celery_app=celery_app,
                timeout=0.1,
            )
        )
        self.assertEqual(control.timeout, 0.1)

    def test_queue_has_consumer_returns_false_for_missing_queue(self):
        from app.celery_readiness import queue_has_consumer

        celery_app = SimpleNamespace(
            control=FakeControl({"celery@pod": [{"name": "celery"}]})
        )

        self.assertFalse(
            queue_has_consumer(
                "tron_usdt_fee_payouts",
                celery_app=celery_app,
                timeout=0.1,
            )
        )

    def test_queue_has_consumer_returns_false_when_inspect_fails(self):
        from app.celery_readiness import queue_has_consumer

        celery_app = SimpleNamespace(control=FakeControl(exc=RuntimeError("down")))

        self.assertFalse(
            queue_has_consumer(
                "tron_usdt_fee_payouts",
                celery_app=celery_app,
                timeout=0.1,
            )
        )


if __name__ == "__main__":
    unittest.main()

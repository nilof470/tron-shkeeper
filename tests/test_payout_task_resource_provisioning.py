from contextlib import contextmanager
from decimal import Decimal
import importlib
import sqlite3
import sys
from types import SimpleNamespace
import unittest

from flask import Flask, g


DESTINATION = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
FEE_DEPOSIT = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
TEST_DATABASE = "/private/tmp/tron-shkeeper-payout-task-tests.db"


def prepare_import_database():
    db = sqlite3.connect(TEST_DATABASE)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS keys (
            symbol TEXT,
            public TEXT,
            private TEXT,
            type TEXT
        )
        """
    )
    db.execute("DELETE FROM keys")
    db.execute(
        "INSERT INTO keys (symbol, public, private, type) VALUES (?, ?, ?, ?)",
        ("_", FEE_DEPOSIT, "EXTERNALLY_MANAGED", "fee_deposit"),
    )
    db.commit()
    db.close()

    from app.config import config

    config.DATABASE = TEST_DATABASE
    for module_name in [
        "app.api.payout",
        "app.api",
        "app.tasks",
        "app.wallet",
    ]:
        sys.modules.pop(module_name, None)


def load_tasks():
    prepare_import_database()
    return importlib.import_module("app.tasks")


def load_payout_module():
    prepare_import_database()
    return importlib.import_module("app.api.payout")


class FakeWallet:
    def __init__(self, symbol, events=None, transfer_result=None):
        self.symbol = symbol
        self.balance = Decimal("100")
        self.client = "tron-client"
        self.events = events
        self.transfer_result = transfer_result or {
            "dest": DESTINATION,
            "status": "success",
            "txids": ["tx-1"],
        }

    def transfer(self, dst, amount):
        if self.events is not None:
            self.events.append(("transfer", dst, amount))
        return dict(self.transfer_result)


class FakeSignature:
    def __init__(self, name, args, calls):
        self.name = name
        self.args = args
        self.calls = calls
        self.options = {}

    def set(self, **kwargs):
        self.options.update(kwargs)
        return self

    def __or__(self, other):
        return FakeChain(self, other, self.calls)


class FakeChain:
    def __init__(self, left, right, calls):
        self.left = left
        self.right = right
        self.calls = calls

    def apply_async(self):
        self.calls.append((self.left, self.right))
        return SimpleNamespace(id="task-1")


class ReleaseFailingRedisLock:
    def __init__(self, events, exc):
        self.events = events
        self.exc = exc

    def acquire(self, blocking=True):
        self.events.append(("redis_lock_acquire", blocking))
        return True

    def release(self):
        self.events.append(("redis_lock_release",))
        raise self.exc


class ReleaseFailingRedisClient:
    def __init__(self, events, exc):
        self.events = events
        self.exc = exc

    def lock(self, *args, **kwargs):
        self.events.append(("redis_lock_create", args, kwargs))
        return ReleaseFailingRedisLock(self.events, self.exc)


class PayoutTaskResourceProvisioningTests(unittest.TestCase):
    def patch_tasks(
        self,
        tasks,
        *,
        enabled=True,
        transfer_result=None,
        replace_lock=True,
    ):
        events = []
        original_config = tasks.config
        original_wallet = tasks.Wallet
        original_helper = tasks.ensure_fee_deposit_resources_for_usdt_payout
        original_lock = tasks.usdt_payout_resource_lock
        original_post_results = tasks.post_payout_results
        task_globals = tasks.payout.run.__globals__
        original_globals_wallet = task_globals["Wallet"]
        original_globals_config = task_globals["config"]
        original_globals_helper = task_globals[
            "ensure_fee_deposit_resources_for_usdt_payout"
        ]
        original_globals_lock = task_globals["usdt_payout_resource_lock"]
        original_globals_post_results = task_globals["post_payout_results"]
        tasks.config = SimpleNamespace(
            TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED=enabled,
            CONCURRENT_MAX_WORKERS=1,
            REDIS_HOST="localhost",
            TRON_USDT_PAYOUT_RESOURCE_LOCK_TTL_SEC=900,
            TRON_USDT_PAYOUT_RESOURCE_LOCK_WAIT_SEC=900,
        )

        fake_wallet_factory = lambda symbol: FakeWallet(
            symbol,
            events=events,
            transfer_result=transfer_result,
        )
        tasks.Wallet = fake_wallet_factory

        def fake_helper(destination, amount, tron_client=None):
            events.append(("ensure", destination, amount, tron_client))

        tasks.ensure_fee_deposit_resources_for_usdt_payout = fake_helper

        if replace_lock:
            @contextmanager
            def fake_lock():
                events.append(("lock_enter",))
                try:
                    yield
                finally:
                    events.append(("lock_exit",))

            tasks.usdt_payout_resource_lock = fake_lock
            task_globals["usdt_payout_resource_lock"] = fake_lock
        posted = []
        tasks.post_payout_results = SimpleNamespace(
            delay=lambda results, symbol: posted.append((results, symbol))
        )
        task_globals["Wallet"] = fake_wallet_factory
        task_globals["config"] = tasks.config
        task_globals["ensure_fee_deposit_resources_for_usdt_payout"] = fake_helper
        task_globals["post_payout_results"] = tasks.post_payout_results

        def restore():
            tasks.config = original_config
            tasks.Wallet = original_wallet
            tasks.ensure_fee_deposit_resources_for_usdt_payout = original_helper
            tasks.usdt_payout_resource_lock = original_lock
            tasks.post_payout_results = original_post_results
            task_globals["Wallet"] = original_globals_wallet
            task_globals["config"] = original_globals_config
            task_globals["ensure_fee_deposit_resources_for_usdt_payout"] = (
                original_globals_helper
            )
            task_globals["usdt_payout_resource_lock"] = original_globals_lock
            task_globals["post_payout_results"] = original_globals_post_results

        return events, posted, restore

    def test_prepare_payout_marks_usdt_single_when_feature_enabled(self):
        tasks = load_tasks()

        events, posted, restore = self.patch_tasks(tasks, enabled=True)
        try:
            steps = tasks.prepare_payout.run(DESTINATION, Decimal("1.25"), "USDT")
        finally:
            restore()

        self.assertEqual(posted, [])
        self.assertEqual(events, [])
        self.assertEqual(steps[0]["dst"], DESTINATION)
        self.assertEqual(steps[0]["amount"], Decimal("1.25"))
        self.assertTrue(steps[0]["ensure_usdt_payout_resources"])

    def test_prepare_multipayout_does_not_mark_resource_provisioning(self):
        tasks = load_tasks()

        steps = tasks.prepare_multipayout.run(
            [{"dest": DESTINATION, "amount": Decimal("1.25")}],
            "USDT",
        )

        self.assertNotIn("ensure_usdt_payout_resources", steps[0])

    def test_payout_calls_resource_helper_before_wallet_transfer(self):
        tasks = load_tasks()

        events, posted, restore = self.patch_tasks(tasks, enabled=True)
        try:
            result = tasks.payout.run(
                [
                    {
                        "dst": DESTINATION,
                        "amount": Decimal("1.25"),
                        "ensure_usdt_payout_resources": True,
                    }
                ],
                "USDT",
            )
        finally:
            restore()

        self.assertEqual(
            events,
            [
                ("lock_enter",),
                ("ensure", DESTINATION, Decimal("1.25"), "tron-client"),
                ("transfer", DESTINATION, Decimal("1.25")),
                ("lock_exit",),
            ],
        )
        self.assertEqual(result[0]["status"], "success")
        self.assertEqual(posted, [(result, "USDT")])

    def test_payout_raises_when_wallet_transfer_returns_error_status(self):
        tasks = load_tasks()

        events, posted, restore = self.patch_tasks(
            tasks,
            enabled=True,
            transfer_result={"dest": DESTINATION, "status": "error"},
        )
        try:
            with self.assertRaisesRegex(Exception, "USDT payout transfer failed"):
                tasks.payout.run(
                    [
                        {
                            "dst": DESTINATION,
                            "amount": Decimal("1.25"),
                            "ensure_usdt_payout_resources": True,
                        }
                    ],
                    "USDT",
                )
        finally:
            restore()

        self.assertEqual(events[0][0], "lock_enter")
        self.assertEqual(events[1][0], "ensure")
        self.assertEqual(events[2][0], "transfer")
        self.assertEqual(events[3][0], "lock_exit")
        self.assertEqual(posted, [])

    def test_payout_posts_result_when_lock_release_connection_fails(self):
        tasks = load_tasks()

        lock_events = []
        original_from_url = tasks.redis.Redis.from_url
        tasks.redis.Redis.from_url = lambda _url: ReleaseFailingRedisClient(
            lock_events,
            tasks.redis.exceptions.ConnectionError("redis down"),
        )
        events, posted, restore = self.patch_tasks(
            tasks,
            enabled=True,
            replace_lock=False,
        )
        try:
            result = tasks.payout.run(
                [
                    {
                        "dst": DESTINATION,
                        "amount": Decimal("1.25"),
                        "ensure_usdt_payout_resources": True,
                    }
                ],
                "USDT",
            )
        finally:
            tasks.redis.Redis.from_url = original_from_url
            restore()

        self.assertEqual(
            events,
            [
                ("ensure", DESTINATION, Decimal("1.25"), "tron-client"),
                ("transfer", DESTINATION, Decimal("1.25")),
            ],
        )
        self.assertEqual(lock_events[0][0], "redis_lock_create")
        self.assertEqual(lock_events[1], ("redis_lock_acquire", True))
        self.assertEqual(lock_events[2], ("redis_lock_release",))
        self.assertEqual(result[0]["status"], "success")
        self.assertEqual(posted, [(result, "USDT")])

    def test_payout_does_not_call_resource_helper_for_non_usdt_path(self):
        tasks = load_tasks()

        events, posted, restore = self.patch_tasks(tasks, enabled=False)
        try:
            result = tasks.payout.run(
                [{"dst": DESTINATION, "amount": Decimal("1.25")}],
                "USDC",
            )
        finally:
            restore()

        self.assertEqual(events, [("transfer", DESTINATION, Decimal("1.25"))])
        self.assertEqual(result[0]["status"], "success")
        self.assertEqual(posted, [(result, "USDC")])

    def test_calc_tx_fee_returns_resource_quote_when_enabled(self):
        payout_module = load_payout_module()

        class Quote:
            def to_dict(self):
                return {"submit_ready": True}

        original_config = payout_module.config
        original_estimate = payout_module.estimate_fee_deposit_resources_for_usdt_payout
        payout_module.config = SimpleNamespace(
            TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED=True,
            TX_FEE=Decimal("40"),
        )
        payout_module.estimate_fee_deposit_resources_for_usdt_payout = (
            lambda destination, amount: Quote()
        )
        app = Flask(__name__)
        try:
            with app.test_request_context(
                f"/USDT/calc-tx-fee/1.25?address={DESTINATION}",
                method="POST",
            ):
                g.symbol = "USDT"
                result = payout_module.calc_tx_fee(Decimal("1.25"))
        finally:
            payout_module.config = original_config
            payout_module.estimate_fee_deposit_resources_for_usdt_payout = (
                original_estimate
            )

        self.assertEqual(result, {"fee": "0", "resource_quote": {"submit_ready": True}})

    def test_api_routes_usdt_single_chain_to_dedicated_queue_when_enabled(self):
        payout_module = load_payout_module()

        calls = []
        original_config = payout_module.config
        original_prepare = payout_module.prepare_payout
        original_payout_task = payout_module.payout_task
        payout_module.config = SimpleNamespace(
            TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED=True,
            TRON_USDT_PAYOUT_QUEUE="tron_usdt_fee_payouts",
        )
        payout_module.prepare_payout = SimpleNamespace(
            s=lambda *args: FakeSignature("prepare", args, calls)
        )
        payout_module.payout_task = SimpleNamespace(
            s=lambda *args: FakeSignature("payout", args, calls)
        )
        app = Flask(__name__)
        try:
            with app.test_request_context(
                f"/USDT/payout/{DESTINATION}/1.25",
                method="POST",
            ):
                g.symbol = "USDT"
                result = payout_module.payout(DESTINATION, Decimal("1.25"))
        finally:
            payout_module.config = original_config
            payout_module.prepare_payout = original_prepare
            payout_module.payout_task = original_payout_task

        self.assertEqual(result, {"task_id": "task-1"})
        prepare_sig, execute_sig = calls[0]
        self.assertEqual(prepare_sig.args, (DESTINATION, Decimal("1.25"), "USDT"))
        self.assertEqual(execute_sig.args, ("USDT",))
        self.assertEqual(
            prepare_sig.options,
            {"queue": "tron_usdt_fee_payouts"},
        )
        self.assertEqual(
            execute_sig.options,
            {"queue": "tron_usdt_fee_payouts"},
        )


if __name__ == "__main__":
    unittest.main()

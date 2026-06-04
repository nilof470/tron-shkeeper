from __future__ import annotations

from decimal import Decimal
import importlib
import os
import sys
from types import SimpleNamespace
import sqlite3
import unittest
from unittest.mock import patch

from flask import Flask


TEST_DATABASE = "/private/tmp/tron-shkeeper-payout-callback-outbox.db"
TEST_BALANCES_DATABASE = "/private/tmp/tron-shkeeper-payout-callback-outbox-balances.db"
DESTINATION = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
FEE_DEPOSIT = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"


def reset_modules():
    for module_name in [
        "app.payout_callback_outbox",
        "app.tasks",
        "app.wallet",
    ]:
        sys.modules.pop(module_name, None)


class FakeWallet:
    client = "tron-client"

    def __init__(self, _symbol):
        self.transfers = []

    def transfer(self, dst, amount):
        self.transfers.append((dst, amount))
        return {"dest": dst, "status": "success", "txids": ["tx-1"]}


class PayoutCallbackOutboxTests(unittest.TestCase):
    def setUp(self):
        if os.path.exists(TEST_DATABASE):
            os.unlink(TEST_DATABASE)
        if os.path.exists(TEST_BALANCES_DATABASE):
            os.unlink(TEST_BALANCES_DATABASE)

        from app.config import config

        config.DATABASE = TEST_DATABASE
        config.BALANCES_DATABASE = TEST_BALANCES_DATABASE
        config.PAYOUT_CALLBACK_MAX_ATTEMPTS = 3
        config.PAYOUT_CALLBACK_RETRY_DELAY_SEC = 1
        config.PAYOUT_CALLBACK_TIMEOUT_SEC = 1
        config.PAYOUT_CALLBACK_SWEEP_ENABLED = True
        config.PAYOUT_CALLBACK_SWEEP_PERIOD_SEC = 1
        config.PAYOUT_CALLBACK_SWEEP_LIMIT = 10
        config.PAYOUT_CALLBACK_CLAIM_TTL_SEC = 60
        reset_modules()

        self.app = Flask(__name__, root_path=os.path.join(os.getcwd(), "app"))
        self.app.config.update(TESTING=True, DATABASE=TEST_DATABASE)
        self.app.config.DATABASE = TEST_DATABASE
        self.app.config.BALANCES_DATABASE = TEST_BALANCES_DATABASE

        from app import db

        db.init_app(self.app)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.get_db().execute(
            """
            INSERT INTO keys (symbol, public, private, type)
            VALUES (?, ?, ?, ?)
            """,
            ("_", FEE_DEPOSIT, "EXTERNALLY_MANAGED", "fee_deposit"),
        )
        db.get_db().commit()

    def tearDown(self):
        self.app_context.pop()

    def test_schema_creates_callback_outbox_and_execution_constraints(self):
        from app.db import get_db

        db = get_db()
        table = db.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'payout_callback_outbox'
            """
        ).fetchone()

        self.assertIsNotNone(table)

        insert = """
            INSERT INTO payout_executions (
                execution_id, consumer, external_id, request_hash,
                sidecar_payload_hash, state, state_version, state_transition_id,
                state_updated_at, source_wallet, token_contract,
                chain_id_or_network_id, canonical_payload_json, payout_queue
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?)
        """
        values = (
            "execution-1",
            "grither-pay",
            "WD-1",
            "request-hash",
            "sidecar-hash",
            "RECEIVED",
            1,
            "transition-1",
            "fee_deposit",
            "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            "main",
            "{}",
            "tron_usdt_fee_payouts",
        )
        db.execute(insert, values)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                insert,
                ("execution-2",) + values[1:7] + ("transition-2",) + values[8:],
            )

    def test_post_payout_results_records_failure_without_sleep_loop(self):
        outbox = importlib.import_module("app.payout_callback_outbox")
        tasks = importlib.import_module("app.tasks")
        outbox_id = outbox.create_payout_callback(
            [{"dest": DESTINATION, "status": "success", "txids": ["tx-1"]}],
            "USDT",
        )

        with patch.object(
            outbox.requests,
            "post",
            side_effect=RuntimeError("shkeeper unavailable"),
        ):
            with patch.object(
                tasks.time,
                "sleep",
                side_effect=AssertionError("callback task must not sleep-loop"),
            ):
                result = tasks.post_payout_results.run(outbox_id)

        stored = outbox.get_payout_callback(outbox_id)
        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(stored["status"], "PENDING")
        self.assertEqual(stored["attempts"], 1)
        self.assertEqual(stored["last_error"], "shkeeper unavailable")
        self.assertIsNotNone(stored["next_attempt_at"])

    def test_due_dispatcher_waits_until_pending_callback_is_due(self):
        from app.config import config

        outbox = importlib.import_module("app.payout_callback_outbox")
        tasks = importlib.import_module("app.tasks")
        outbox_id = outbox.create_payout_callback(
            [{"dest": DESTINATION, "status": "success", "txids": ["tx-1"]}],
            "USDT",
        )
        original_retry_delay = config.PAYOUT_CALLBACK_RETRY_DELAY_SEC
        config.PAYOUT_CALLBACK_RETRY_DELAY_SEC = 3600

        try:
            with patch.object(
                outbox.requests,
                "post",
                side_effect=RuntimeError("shkeeper unavailable"),
            ):
                result = tasks.post_payout_results.run(outbox_id)
        finally:
            config.PAYOUT_CALLBACK_RETRY_DELAY_SEC = original_retry_delay

        stored = outbox.get_payout_callback(outbox_id)
        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(stored["status"], "PENDING")
        self.assertEqual(stored["last_error"], "shkeeper unavailable")

        with patch.object(outbox.requests, "post") as post:
            results = tasks.dispatch_due_payout_callbacks.run(limit=10)

        self.assertEqual(results, [])
        post.assert_not_called()

    def test_queue_payout_callback_keeps_outbox_when_task_enqueue_fails(self):
        tasks = importlib.import_module("app.tasks")
        outbox = importlib.import_module("app.payout_callback_outbox")

        with patch.object(
            tasks.post_payout_results,
            "delay",
            side_effect=RuntimeError("redis unavailable"),
        ):
            outbox_id = tasks.queue_payout_callback(
                [{"dest": DESTINATION, "status": "success", "txids": ["tx-1"]}],
                "USDT",
            )

        stored = outbox.get_payout_callback(outbox_id)
        self.assertEqual(stored["status"], "PENDING")
        self.assertEqual(stored["symbol"], "USDT")
        self.assertIsNotNone(stored["next_attempt_at"])

    def test_due_dispatcher_recovers_callback_after_enqueue_failure(self):
        tasks = importlib.import_module("app.tasks")
        outbox = importlib.import_module("app.payout_callback_outbox")

        with patch.object(
            tasks.post_payout_results,
            "delay",
            side_effect=RuntimeError("redis unavailable"),
        ):
            outbox_id = tasks.queue_payout_callback(
                [{"dest": DESTINATION, "status": "success", "txids": ["tx-1"]}],
                "USDT",
            )

        response = SimpleNamespace(status_code=200, text="accepted")
        with patch.object(outbox.requests, "post", return_value=response) as post:
            results = tasks.dispatch_due_payout_callbacks.run(limit=10)

        stored = outbox.get_payout_callback(outbox_id)
        self.assertEqual(stored["status"], "SENT")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "SENT")
        post.assert_called_once()

    def test_claim_prevents_duplicate_concurrent_delivery(self):
        tasks = importlib.import_module("app.tasks")
        outbox = importlib.import_module("app.payout_callback_outbox")
        outbox_id = outbox.create_payout_callback(
            [{"dest": DESTINATION, "status": "success", "txids": ["tx-1"]}],
            "USDT",
        )
        claimed = outbox.claim_payout_callback(outbox_id, claim_token="worker-1")
        self.assertEqual(claimed["status"], "DISPATCHING")

        with patch.object(outbox.requests, "post") as post:
            result = tasks.post_payout_results.run(outbox_id)

        self.assertEqual(result["status"], "DISPATCHING")
        post.assert_not_called()

    def test_payout_worker_records_outbox_and_finishes_before_notification(self):
        tasks = importlib.import_module("app.tasks")
        outbox = importlib.import_module("app.payout_callback_outbox")
        scheduled = []
        queue_callback = tasks.payout.run.__globals__["queue_payout_callback"]
        original_wallet = tasks.Wallet
        original_task_wallet = tasks.payout.run.__globals__["Wallet"]
        original_post_task = tasks.post_payout_results
        original_queue_post_task = queue_callback.__globals__["post_payout_results"]

        try:
            tasks.Wallet = FakeWallet
            tasks.payout.run.__globals__["Wallet"] = FakeWallet
            tasks.post_payout_results = SimpleNamespace(
                delay=lambda outbox_id: scheduled.append(outbox_id)
            )
            queue_callback.__globals__["post_payout_results"] = tasks.post_payout_results
            result = tasks.payout.run(
                [{"dst": DESTINATION, "amount": Decimal("1.25")}],
                "USDT",
            )
        finally:
            tasks.Wallet = original_wallet
            tasks.payout.run.__globals__["Wallet"] = original_task_wallet
            tasks.post_payout_results = original_post_task
            queue_callback.__globals__["post_payout_results"] = original_queue_post_task

        self.assertEqual(result[0]["status"], "success")
        self.assertEqual(len(scheduled), 1)
        stored = outbox.get_payout_callback(scheduled[0])
        self.assertEqual(stored["status"], "PENDING")
        self.assertEqual(stored["symbol"], "USDT")
        self.assertIn("tx-1", stored["payload_json"])

    def test_payout_worker_returns_success_when_outbox_write_fails_after_transfer(self):
        tasks = importlib.import_module("app.tasks")
        queue_callback = tasks.payout.run.__globals__["queue_payout_callback"]
        original_wallet = tasks.Wallet
        original_task_wallet = tasks.payout.run.__globals__["Wallet"]
        original_create = tasks.create_payout_callback
        original_queue_create = queue_callback.__globals__["create_payout_callback"]

        try:
            tasks.Wallet = FakeWallet
            tasks.payout.run.__globals__["Wallet"] = FakeWallet
            tasks.create_payout_callback = lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("database locked")
            )
            queue_callback.__globals__["create_payout_callback"] = (
                tasks.create_payout_callback
            )
            result = tasks.payout.run(
                [{"dst": DESTINATION, "amount": Decimal("1.25")}],
                "USDT",
            )
        finally:
            tasks.Wallet = original_wallet
            tasks.payout.run.__globals__["Wallet"] = original_task_wallet
            tasks.create_payout_callback = original_create
            queue_callback.__globals__["create_payout_callback"] = original_queue_create

        self.assertEqual(result[0]["status"], "success")
        self.assertEqual(result[0]["txids"], ["tx-1"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
import unittest

import prometheus_client
from flask import Flask


TEST_DATABASE = "/private/tmp/tron-shkeeper-payout-metrics.db"
TEST_BALANCES_DATABASE = "/private/tmp/tron-shkeeper-payout-metrics-balances.db"


class FakeRedis:
    def __init__(self, depth, messages=None):
        self.depth = depth
        self.messages = messages or []

    def llen(self, queue):
        return self.depth

    def lrange(self, queue, start, end):
        if start == 0 and end == 0:
            return self.messages[:1]
        if start == -1 and end == -1:
            return self.messages[-1:]
        return self.messages[start : end + 1]


def redis_message(enqueued_at):
    return json.dumps({"headers": {"payout_enqueued_at": enqueued_at}}).encode("utf-8")


class TronPayoutMetricsTests(unittest.TestCase):
    def setUp(self):
        for path in (TEST_DATABASE, TEST_BALANCES_DATABASE):
            if os.path.exists(path):
                os.unlink(path)

        from app.config import config

        config.DATABASE = TEST_DATABASE
        config.BALANCES_DATABASE = TEST_BALANCES_DATABASE
        config.TRON_USDT_PAYOUT_QUEUE = "tron_usdt_fee_payouts"

        self.app = Flask(__name__, root_path=os.path.join(os.getcwd(), "app"))
        self.app.config.update(TESTING=True, DATABASE=TEST_DATABASE)
        self.app.config.DATABASE = TEST_DATABASE
        self.app.config.BALANCES_DATABASE = TEST_BALANCES_DATABASE

        from app import db

        db.init_app(self.app)
        self.app_context = self.app.app_context()
        self.app_context.push()

        import app.api.metrics as metrics
        import app.celery_readiness as celery_readiness

        self.metrics = metrics
        self.celery_readiness = celery_readiness
        self.original_worker_ready = celery_readiness.usdt_payout_worker_ready
        self.original_redis_from_url = metrics.redis.Redis.from_url
        self.original_wallet_balance = metrics._tron_wallet_balance
        celery_readiness.usdt_payout_worker_ready = lambda: False
        metrics.redis.Redis.from_url = lambda *args, **kwargs: FakeRedis(0)
        metrics._tron_wallet_balance = lambda symbol: {
            "USDT": Decimal("123.456"),
            "TRX": Decimal("78.9"),
        }[symbol]
        metrics._clear_payout_metrics()

    def tearDown(self):
        self.celery_readiness.usdt_payout_worker_ready = self.original_worker_ready
        self.metrics.redis.Redis.from_url = self.original_redis_from_url
        self.metrics._tron_wallet_balance = self.original_wallet_balance
        self.metrics._clear_payout_metrics()
        self.app_context.pop()
        for path in (TEST_DATABASE, TEST_BALANCES_DATABASE):
            if os.path.exists(path):
                os.unlink(path)

    def insert_execution(
        self,
        execution_id,
        state,
        updated_at,
        reconciliation_required=0,
        failure_class=None,
        error_code=None,
    ):
        from app.db import get_db

        get_db().execute(
            """
            INSERT INTO payout_executions (
                execution_id, consumer, external_id, request_hash,
                sidecar_payload_hash, state, state_version, state_transition_id,
                state_updated_at, source_wallet, token_contract,
                chain_id_or_network_id, canonical_payload_json, payout_queue,
                reconciliation_required, failure_class, error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                "grither-pay",
                f"WD-{execution_id}",
                f"request-{execution_id}",
                f"sidecar-{execution_id}",
                state,
                1,
                f"transition-{execution_id}",
                updated_at,
                "fee_deposit",
                "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                "main",
                "{}",
                "tron_usdt_fee_payouts",
                reconciliation_required,
                failure_class,
                error_code,
            ),
        )
        get_db().commit()

    def insert_callback(self, status, created_at):
        from app.db import get_db

        get_db().execute(
            """
            INSERT INTO payout_callback_outbox (
                symbol, payload_json, status, attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("USDT", "[]", status, 1, created_at, created_at),
        )
        get_db().commit()

    def test_payout_metrics_expose_execution_outbox_and_worker_readiness(self):
        now = datetime(2026, 6, 4, 12, 0, 0)
        self.insert_execution(
            "created-1",
            "RECEIVED",
            (now - timedelta(minutes=10)).isoformat() + "Z",
        )
        self.insert_execution(
            "created-2",
            "RECEIVED",
            (now - timedelta(minutes=5)).isoformat() + "Z",
        )
        self.insert_execution(
            "recon-1",
            "RECONCILIATION_REQUIRED",
            (now - timedelta(minutes=30)).isoformat() + "Z",
            reconciliation_required=1,
        )
        self.insert_execution(
            "confirmed-1",
            "CONFIRMED",
            (now - timedelta(hours=2)).isoformat() + "Z",
        )
        self.insert_callback("RETRY", (now - timedelta(minutes=20)).isoformat() + "Z")
        self.metrics.redis.Redis.from_url = lambda *args, **kwargs: FakeRedis(
            7,
            messages=[
                redis_message((now - timedelta(minutes=5)).isoformat() + "Z"),
                redis_message((now - timedelta(minutes=15)).isoformat() + "Z"),
            ],
        )

        self.metrics.update_payout_metrics(now=now)

        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_execution_count{reconciliation_required="false",state="RECEIVED"} 2.0',
            text,
        )
        self.assertIn(
            'tron_payout_non_terminal_oldest_age_seconds{state="RECEIVED"} 600.0',
            text,
        )
        self.assertIn("tron_payout_reconciliation_required_count 1.0", text)
        self.assertIn(
            'tron_payout_callback_outbox_backlog_count{status="RETRY"} 1.0',
            text,
        )
        self.assertIn(
            'tron_payout_callback_outbox_oldest_age_seconds{status="RETRY"} 1200.0',
            text,
        )
        self.assertIn(
            'tron_payout_worker_ready{queue="tron_usdt_fee_payouts"} 0.0',
            text,
        )
        self.assertIn(
            'tron_payout_broker_queue_depth{queue="tron_usdt_fee_payouts"} 7.0',
            text,
        )
        self.assertIn(
            'tron_payout_broker_queue_oldest_age_seconds{queue="tron_usdt_fee_payouts"} 900.0',
            text,
        )
        self.assertIn(
            'tron_payout_hot_wallet_balance{asset="USDT",source_wallet="fee_deposit"} 123.456',
            text,
        )
        self.assertIn(
            'tron_payout_fee_wallet_balance{asset="TRX",source_wallet="fee_deposit"} 78.9',
            text,
        )
        self.assertNotIn(
            'tron_payout_non_terminal_oldest_age_seconds{state="CONFIRMED"}',
            text,
        )

    def test_payout_failure_metrics_bound_error_code_labels(self):
        now = datetime(2026, 6, 4, 12, 0, 0)
        self.insert_execution(
            "failed-preflight",
            "FAILED_PRE_BROADCAST",
            (now - timedelta(minutes=5)).isoformat() + "Z",
            failure_class="PREFLIGHT",
            error_code="INSUFFICIENT_USDT",
        )
        self.insert_execution(
            "failed-weird-error",
            "FAILED_PRE_BROADCAST",
            (now - timedelta(minutes=4)).isoformat() + "Z",
            failure_class="PREFLIGHT",
            error_code="error with destination TRxxxx",
        )

        self.metrics.update_payout_metrics(now=now)

        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_failure_count{error_code="INSUFFICIENT_USDT",failure_class="PREFLIGHT",state="FAILED_PRE_BROADCAST"} 1.0',
            text,
        )
        self.assertIn(
            'tron_payout_failure_count{error_code="OTHER",failure_class="PREFLIGHT",state="FAILED_PRE_BROADCAST"} 1.0',
            text,
        )
        self.assertNotIn("TRxxxx", text)

    def test_release_lookup_fails_open(self):
        original_get = self.metrics.requests.get
        self.metrics.get_latest_release.cache_clear()
        self.metrics.requests.get = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("github unavailable")
        )
        try:
            release = self.metrics.get_latest_release(ttl_hash=1)
        finally:
            self.metrics.requests.get = original_get
            self.metrics.get_latest_release.cache_clear()

        self.assertEqual("unknown", release["version"])

    def test_metrics_endpoint_keeps_payout_metrics_when_chain_metrics_fail(self):
        original_block_scanner = self.metrics.BlockScanner
        self.metrics.BlockScanner = lambda: (_ for _ in ()).throw(
            RuntimeError("fullnode unavailable")
        )
        try:
            text = self.metrics.get_metrics()
        finally:
            self.metrics.BlockScanner = original_block_scanner

        self.assertIn("tron_has_alive_servers 0.0", text)
        self.assertIn(
            'tron_payout_worker_ready{queue="tron_usdt_fee_payouts"} 0.0',
            text,
        )

    def test_broker_queue_depth_fails_open_when_redis_is_unavailable(self):
        self.metrics.redis.Redis.from_url = lambda *args, **kwargs: (
            _ for _ in ()
        ).throw(self.metrics.redis.exceptions.ConnectionError("redis down"))

        self.metrics.update_payout_metrics(now=datetime(2026, 6, 4, 12, 0, 0))

        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_broker_queue_depth{queue="tron_usdt_fee_payouts"} -1.0',
            text,
        )
        self.assertIn(
            'tron_payout_broker_queue_oldest_age_seconds{queue="tron_usdt_fee_payouts"} -1.0',
            text,
        )

    def test_wallet_balance_metrics_fail_open_when_balance_collection_fails(self):
        self.metrics._tron_wallet_balance = lambda symbol: (_ for _ in ()).throw(
            RuntimeError(f"{symbol} balance unavailable")
        )

        self.metrics.update_payout_metrics(now=datetime(2026, 6, 4, 12, 0, 0))

        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_hot_wallet_balance{asset="USDT",source_wallet="fee_deposit"} -1.0',
            text,
        )
        self.assertIn(
            'tron_payout_fee_wallet_balance{asset="TRX",source_wallet="fee_deposit"} -1.0',
            text,
        )

    def test_broker_queue_age_fails_open_when_message_is_unparseable(self):
        self.metrics.redis.Redis.from_url = lambda *args, **kwargs: FakeRedis(
            2,
            messages=[b"\xff"],
        )

        self.metrics.update_payout_metrics(now=datetime(2026, 6, 4, 12, 0, 0))

        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_broker_queue_depth{queue="tron_usdt_fee_payouts"} 2.0',
            text,
        )
        self.assertIn(
            'tron_payout_broker_queue_oldest_age_seconds{queue="tron_usdt_fee_payouts"} -1.0',
            text,
        )

    def test_worker_and_queue_metrics_survive_db_collection_failure(self):
        import app.db as db_module

        now = datetime(2026, 6, 4, 12, 0, 0)
        self.insert_execution(
            "recon-snapshot",
            "RECONCILIATION_REQUIRED",
            (now - timedelta(minutes=30)).isoformat() + "Z",
            reconciliation_required=1,
        )
        self.metrics.update_payout_metrics(now=now)

        original_get_db = db_module.get_db
        db_module.get_db = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
        self.metrics.redis.Redis.from_url = lambda *args, **kwargs: FakeRedis(
            3,
            messages=[
                redis_message((now - timedelta(minutes=4)).isoformat() + "Z"),
            ],
        )
        try:
            with self.assertRaises(RuntimeError):
                self.metrics.update_payout_metrics(
                    now=now + timedelta(minutes=1)
                )
        finally:
            db_module.get_db = original_get_db

        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_execution_count{reconciliation_required="true",state="RECONCILIATION_REQUIRED"} 1.0',
            text,
        )
        self.assertIn(
            'tron_payout_worker_ready{queue="tron_usdt_fee_payouts"} 0.0',
            text,
        )
        self.assertIn(
            'tron_payout_broker_queue_depth{queue="tron_usdt_fee_payouts"} 3.0',
            text,
        )
        self.assertIn(
            'tron_payout_broker_queue_oldest_age_seconds{queue="tron_usdt_fee_payouts"} 300.0',
            text,
        )

    def test_enqueue_execution_sets_broker_age_header(self):
        from app import tasks
        from app.payout_execution import PayoutExecutionStore

        calls = []

        class FakeTask:
            def apply_async(self, **kwargs):
                calls.append(kwargs)
                return "task-result"

        result = PayoutExecutionStore.enqueue_execution(
            "exec-1",
            "queue-a",
            task=FakeTask(),
        )

        self.assertEqual(result, "task-result")
        self.assertEqual(calls[0]["args"], ["exec-1"])
        self.assertEqual(calls[0]["queue"], "queue-a")
        self.assertIn("payout_enqueued_at", calls[0]["headers"])


if __name__ == "__main__":
    unittest.main()

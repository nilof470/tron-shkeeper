from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import hashlib
import importlib
import json
import os
import sys
import unittest
from unittest.mock import patch

from flask import Flask


TEST_DATABASE = "/private/tmp/tron-shkeeper-payout-execution-boundaries.db"
TEST_BALANCES_DATABASE = "/private/tmp/tron-shkeeper-payout-execution-boundaries-balances.db"
DESTINATION = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"


def reset_database():
    if os.path.exists(TEST_DATABASE):
        os.unlink(TEST_DATABASE)
    if os.path.exists(TEST_BALANCES_DATABASE):
        os.unlink(TEST_BALANCES_DATABASE)

    from app.config import config

    config.DATABASE = TEST_DATABASE
    config.BALANCES_DATABASE = TEST_BALANCES_DATABASE
    config.PAYOUT_EXECUTION_AUTO_ENQUEUE_ENABLED = True
    config.PAYOUT_EXECUTION_LEASE_TTL_SEC = 300
    for module_name in [
        "app.payout_execution",
        "app.tasks",
        "app.wallet",
    ]:
        sys.modules.pop(module_name, None)


def compact_json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_sidecar_payload(payload):
    amount = Decimal(str(payload["amount"])).quantize(Decimal("0.000001"))
    return {
        "consumer": payload["consumer"],
        "execution_id": payload["execution_id"],
        "external_id": payload["external_id"],
        "asset": payload["asset"],
        "network": payload["network"],
        "amount": format(amount, "f"),
        "destination": payload["destination"],
        "contract_version": payload["contract_version"],
    }


class FakeTx:
    txid = "tx-1"

    def to_json(self):
        return {
            "txID": self.txid,
            "raw_data": {
                "contract": [],
                "expiration": 1_780_000_000_000,
                "ref_block_bytes": "abcd",
                "ref_block_hash": "1234567890abcdef",
            },
            "signature": ["signature-1"],
        }


class BoundaryWallet:
    client = "tron-client"

    def __init__(
        self,
        events,
        row_reader,
        broadcast_exc=None,
        balance=Decimal("100"),
        broadcast_txid="tx-1",
    ):
        self.events = events
        self.row_reader = row_reader
        self.broadcast_exc = broadcast_exc
        self.balance = balance
        self.broadcast_txid = broadcast_txid

    def build_signed_transfer(self, destination, amount, *, expiration_ms=None):
        row = self.row_reader()
        self.events.append(("build_signed_transfer", destination, amount))
        assert row["resource_reservation_id"]
        assert row["signed_raw_tx_hash"] is None
        assert row["broadcast_attempted_at"] is None
        return FakeTx()

    def signed_transfer_evidence(self, tx):
        raw = compact_json(tx.to_json())
        signed_hash = sha256_hex(raw)
        return {
            "signed_raw_tx_ref": f"not-retained:signed-tron-tx-sha256:{signed_hash}",
            "signed_raw_tx_hash": signed_hash,
            "txid": tx.txid,
            "reference_block": "abcd:1234567890abcdef",
            "expiration_at": "2026-05-31T16:53:20.000000Z",
            "chain_check_metadata": {
                "signed_tx_artifact_retention": "NOT_RETAINED_SPENDABLE_RAW_TX",
                "signed_tx_hash_algorithm": "sha256",
                "signed_tx_json_hash": signed_hash,
                "signed_tx_txid": tx.txid,
            },
        }

    def broadcast_signed_transfer(self, tx):
        row = self.row_reader()
        self.events.append(("broadcast", tx.txid))
        assert row["state"] == "BROADCASTING"
        assert row["signed_raw_tx_hash"]
        assert row["broadcast_attempted_at"]
        if self.broadcast_exc:
            raise self.broadcast_exc
        return {"txid": self.broadcast_txid, "receipt": {"result": "SUCCESS"}}


class PayoutExecutionBoundariesTests(unittest.TestCase):
    def setUp(self):
        reset_database()
        self.app = Flask(__name__, root_path=os.path.join(os.getcwd(), "app"))
        self.app.config.update(TESTING=True, DATABASE=TEST_DATABASE)
        self.app.config.DATABASE = TEST_DATABASE
        self.app.config.BALANCES_DATABASE = TEST_BALANCES_DATABASE

        from app import db

        db.init_app(self.app)
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.store_module = importlib.import_module("app.payout_execution")

    def tearDown(self):
        self.app_context.pop()

    def body(self, **overrides):
        payload = {
            "consumer": "grither-pay",
            "execution_id": "1",
            "external_id": "WD-1",
            "asset": "USDT",
            "network": "TRON",
            "amount": "25",
            "destination": DESTINATION,
            "contract_version": "usdt-payout-execution-v1",
            "request_hash": "request-hash",
            "source_wallet_ref": "fee_deposit",
            "payout_queue": "tron_usdt_fee_payouts",
        }
        payload.update(overrides)
        payload["sidecar_payload_hash"] = sha256_hex(
            compact_json(canonical_sidecar_payload(payload))
        )
        return payload

    def submit(self, _mock_enqueue=True, **overrides):
        if not _mock_enqueue:
            return self.store_module.PayoutExecutionStore.submit(
                self.body(**overrides),
                authenticated_consumer="grither-pay",
            )
        with patch.object(
            self.store_module.PayoutExecutionStore,
            "enqueue_execution",
            return_value=None,
        ):
            return self.store_module.PayoutExecutionStore.submit(
                self.body(**overrides),
                authenticated_consumer="grither-pay",
            )

    def row(self, execution_id="1"):
        return self.store_module.PayoutExecutionStore._get_row(execution_id)

    def set_execution_fields(self, **fields):
        assignments = ", ".join(f"{field} = ?" for field in fields)
        values = list(fields.values()) + ["1"]
        from app.db import get_db

        db = get_db()
        db.execute(
            f"""
            UPDATE payout_executions
            SET {assignments}
            WHERE execution_id = ?
            """,
            values,
        )
        db.commit()

    def test_stale_signing_without_side_effects_is_safe_to_retry(self):
        self.submit()
        self.set_execution_fields(
            state="SIGNING",
            lease_expires_at="2026-01-01T00:00:00.000000Z",
        )

        status = self.store_module.PayoutExecutionStore.recover_stale_signing("1")

        self.assertEqual(status["state"], "RECEIVED")
        self.assertFalse(status["reconciliation_required"])

    def test_active_signing_lease_is_not_recovered(self):
        self.submit()
        self.set_execution_fields(
            state="SIGNING",
            lease_owner="worker-active",
            lease_expires_at="2999-01-01T00:00:00.000000Z",
        )

        status = self.store_module.PayoutExecutionStore.recover_stale_signing("1")

        self.assertEqual(status["state"], "SIGNING")
        self.assertEqual(status["lease_owner"], "worker-active")

    def test_execute_does_not_steal_active_signing_lease(self):
        self.submit()
        self.set_execution_fields(
            state="SIGNING",
            lease_owner="worker-active",
            lease_expires_at="2999-01-01T00:00:00.000000Z",
        )
        events = []

        status = self.store_module.PayoutExecutionStore.execute(
            "1",
            wallet=BoundaryWallet(events, self.row),
            resource_ensurer=lambda *args, **kwargs: events.append(("resource",)),
            lease_owner="worker-2",
        )

        self.assertEqual(status["state"], "SIGNING")
        self.assertEqual(events, [])

    def test_stale_signing_with_only_resource_reservation_is_safe_to_retry(self):
        self.submit()
        self.set_execution_fields(
            state="SIGNING",
            lease_expires_at="2026-01-01T00:00:00.000000Z",
            resource_reservation_id="resource-1",
        )

        status = self.store_module.PayoutExecutionStore.recover_stale_signing("1")

        self.assertEqual(status["state"], "RECEIVED")
        self.assertFalse(status["reconciliation_required"])
        self.assertIsNone(status["lease_owner"])
        self.assertIsNone(status["attempt_id"])

    def test_stale_signing_with_signed_artifact_requires_reconciliation(self):
        self.submit()
        self.set_execution_fields(
            state="SIGNING",
            lease_expires_at="2026-01-01T00:00:00.000000Z",
            signed_raw_tx_ref="signed-ref",
            signed_raw_tx_hash="signed-hash",
        )

        status = self.store_module.PayoutExecutionStore.recover_stale_signing("1")

        self.assertEqual(status["state"], "RECONCILIATION_REQUIRED")
        self.assertTrue(status["reconciliation_required"])

    def test_stale_signing_with_broadcast_marker_requires_reconciliation(self):
        self.submit()
        self.set_execution_fields(
            state="SIGNING",
            lease_expires_at="2026-01-01T00:00:00.000000Z",
            broadcast_attempted_at="2026-06-03T00:00:00Z",
        )

        status = self.store_module.PayoutExecutionStore.recover_stale_signing("1")

        self.assertEqual(status["state"], "RECONCILIATION_REQUIRED")
        self.assertTrue(status["reconciliation_required"])

    def test_status_recovers_stale_signed_to_reconciliation(self):
        self.submit()
        self.set_execution_fields(
            state="SIGNED",
            lease_expires_at="2026-01-01T00:00:00.000000Z",
            resource_reservation_id="resource-1",
            signed_raw_tx_ref="not-retained:signed-tron-tx-sha256:abc",
            signed_raw_tx_hash="abc",
            txid="tx-1",
        )

        status = self.store_module.PayoutExecutionStore.status(
            "1",
            authenticated_consumer="grither-pay",
        )

        self.assertEqual(status["state"], "RECONCILIATION_REQUIRED")
        self.assertTrue(status["reconciliation_required"])
        self.assertEqual(status["error_code"], "STALE_SIGNED_WITH_SIDE_EFFECT")

    def test_wallet_broadcast_result_preserves_tronpy_txid(self):
        wallet_module = importlib.import_module("app.wallet")

        class FakeTransactionRet(dict):
            txid = "tx-1"

            def wait(self):
                return {"receipt": {"result": "SUCCESS"}}

        class FakeSignedTransaction:
            def broadcast(self):
                return FakeTransactionRet({"txid": "tx-1"})

        result = wallet_module.Wallet.broadcast_signed_transfer(FakeSignedTransaction())

        self.assertEqual(result["txid"], "tx-1")
        self.assertEqual(result["broadcast_txid"], "tx-1")
        self.assertEqual(result["receipt"], {"result": "SUCCESS"})

    def test_execute_persists_markers_before_external_side_effects(self):
        self.submit()
        events = []

        def resource_ensurer(destination, amount, tron_client=None):
            row = self.row()
            events.append(("resource_ensurer", destination, amount, tron_client))
            self.assertEqual(row["state"], "SIGNING")
            self.assertTrue(row["resource_reservation_id"])
            self.assertIsNone(row["signed_raw_tx_hash"])

        @contextmanager
        def lock_factory():
            events.append(("lock_enter",))
            yield
            events.append(("lock_exit",))

        status = self.store_module.PayoutExecutionStore.execute(
            "1",
            wallet=BoundaryWallet(events, self.row),
            resource_ensurer=resource_ensurer,
            lock_factory=lock_factory,
            lease_owner="worker-1",
        )

        self.assertEqual(
            events,
            [
                ("lock_enter",),
                ("resource_ensurer", DESTINATION, Decimal("25.000000"), "tron-client"),
                ("build_signed_transfer", DESTINATION, Decimal("25.000000")),
                ("broadcast", "tx-1"),
                ("lock_exit",),
            ],
        )
        self.assertEqual(status["state"], "BROADCASTED")
        self.assertEqual(status["txids"], ["tx-1"])
        self.assertEqual(status["txid"], "tx-1")
        self.assertEqual(status["reference_block"], "abcd:1234567890abcdef")
        self.assertEqual(status["expiration_at"], "2026-05-31T16:53:20.000000Z")
        self.assertTrue(status["signed_raw_tx_ref"].startswith("not-retained:"))
        self.assertEqual(
            status["chain_check_metadata"]["signed_tx_artifact_retention"],
            "NOT_RETAINED_SPENDABLE_RAW_TX",
        )

    def test_resource_ensurer_failure_after_marker_is_pre_broadcast_failure(self):
        self.submit()
        events = []

        def resource_ensurer(destination, amount, tron_client=None):
            row = self.row()
            events.append(("resource_ensurer", destination, amount, tron_client))
            self.assertEqual(row["state"], "SIGNING")
            self.assertTrue(row["resource_reservation_id"])
            self.assertIsNone(row["signed_raw_tx_hash"])
            raise self.store_module.PayoutExecutionError(
                "Unable to verify TRON USDT payout resources",
                code="PAYOUT_RESOURCES_UNAVAILABLE",
                status_code=503,
            )

        status = self.store_module.PayoutExecutionStore.execute(
            "1",
            wallet=BoundaryWallet(events, self.row),
            resource_ensurer=resource_ensurer,
            lease_owner="worker-1",
        )

        self.assertEqual(status["state"], "FAILED_PRE_BROADCAST")
        self.assertFalse(status["reconciliation_required"])
        self.assertEqual(status["failure_class"], "PREFLIGHT")
        self.assertEqual(status["error_code"], "PAYOUT_RESOURCES_UNAVAILABLE")
        self.assertEqual(
            events,
            [("resource_ensurer", DESTINATION, Decimal("25.000000"), "tron-client")],
        )
        row = self.row()
        self.assertTrue(row["resource_reservation_id"])
        self.assertIsNone(row["signed_raw_tx_hash"])
        self.assertIsNone(row["broadcast_attempted_at"])

    def test_broadcast_timeout_after_signed_marker_requires_reconciliation(self):
        self.submit()
        events = []

        @contextmanager
        def lock_factory():
            yield

        status = self.store_module.PayoutExecutionStore.execute(
            "1",
            wallet=BoundaryWallet(events, self.row, broadcast_exc=TimeoutError("timeout")),
            resource_ensurer=lambda *args, **kwargs: None,
            lock_factory=lock_factory,
            lease_owner="worker-1",
        )

        self.assertEqual(status["state"], "RECONCILIATION_REQUIRED")
        self.assertTrue(status["reconciliation_required"])
        self.assertEqual(status["error_code"], "UNSAFE_EXECUTION_INTERRUPTED")
        self.assertEqual(status["txid"], "tx-1")
        self.assertTrue(status["signed_raw_tx_hash"])
        self.assertTrue(status["broadcast_attempted_at"])

    def test_broadcast_txid_mismatch_requires_reconciliation(self):
        self.submit()
        events = []

        @contextmanager
        def lock_factory():
            yield

        status = self.store_module.PayoutExecutionStore.execute(
            "1",
            wallet=BoundaryWallet(events, self.row, broadcast_txid="tx-other"),
            resource_ensurer=lambda *args, **kwargs: None,
            lock_factory=lock_factory,
            lease_owner="worker-1",
        )

        self.assertEqual(status["state"], "RECONCILIATION_REQUIRED")
        self.assertTrue(status["reconciliation_required"])
        self.assertEqual(status["error_code"], "BROADCAST_TXID_MISMATCH")
        self.assertEqual(status["txid"], "tx-1")
        self.assertTrue(status["broadcast_attempted_at"])

    def test_execute_rechecks_balance_before_resource_or_signing(self):
        self.submit()
        events = []

        status = self.store_module.PayoutExecutionStore.execute(
            "1",
            wallet=BoundaryWallet(events, self.row, balance=Decimal("1")),
            resource_ensurer=lambda *args, **kwargs: events.append(("resource",)),
            lease_owner="worker-1",
        )

        self.assertEqual(status["state"], "FAILED_PRE_BROADCAST")
        self.assertEqual(status["error_code"], "INSUFFICIENT_USDT")
        self.assertIn("does not have enough USDT", status["error_message"])
        self.assertEqual(events, [])

    def test_resource_lock_timeout_is_retryable_without_side_effects(self):
        self.submit()
        events = []

        @contextmanager
        def unavailable_lock():
            raise self.store_module.PayoutExecutionError(
                "Timed out waiting for TRON USDT payout resource lock",
                code="PAYOUT_RESOURCE_LOCK_UNAVAILABLE",
                status_code=503,
            )
            yield

        status = self.store_module.PayoutExecutionStore.execute(
            "1",
            wallet=BoundaryWallet(events, self.row),
            resource_ensurer=lambda *args, **kwargs: events.append(("resource",)),
            lock_factory=unavailable_lock,
            lease_owner="worker-1",
        )

        self.assertEqual(status["state"], "RECEIVED")
        self.assertEqual(status["failure_class"], "TRANSIENT")
        self.assertEqual(status["error_code"], "PAYOUT_RESOURCE_LOCK_UNAVAILABLE")
        self.assertEqual(events, [])

    def test_execute_reloads_row_after_lock_before_side_effects(self):
        self.submit()
        events = []

        @contextmanager
        def lock_factory():
            self.set_execution_fields(
                state="BROADCASTED",
                resource_reservation_id="resource-1",
                signed_raw_tx_ref="not-retained:signed-tron-tx-sha256:abc",
                signed_raw_tx_hash="abc",
                txid="tx-1",
                txids_json='["tx-1"]',
            )
            yield

        status = self.store_module.PayoutExecutionStore.execute(
            "1",
            wallet=BoundaryWallet(events, self.row),
            resource_ensurer=lambda *args, **kwargs: events.append(("resource",)),
            lock_factory=lock_factory,
            lease_owner="worker-1",
        )

        self.assertEqual(status["state"], "BROADCASTED")
        self.assertEqual(status["txid"], "tx-1")
        self.assertEqual(events, [])

    def test_execute_returns_current_state_when_pre_lock_transition_hits_cas(self):
        self.submit()
        events = []
        store = self.store_module.PayoutExecutionStore
        exc = self.store_module.PayoutExecutionError(
            "Payout execution state changed concurrently",
            code="PAYOUT_EXECUTION_CAS_CONFLICT",
            status_code=409,
        )

        def racing_transition(cls, row, state, **fields):
            if state == "VALIDATED":
                self.set_execution_fields(
                    state="SIGNING",
                    state_version=2,
                    lease_owner="worker-other",
                    lease_expires_at="2999-01-01T00:00:00.000000Z",
                    attempt_id="attempt-other",
                )
                raise exc
            raise AssertionError("race test should only touch first transition")

        with patch.object(store, "_transition", classmethod(racing_transition)):
            status = store.execute(
                "1",
                wallet=BoundaryWallet(events, self.row),
                resource_ensurer=lambda *args, **kwargs: events.append(("resource",)),
                lease_owner="worker-1",
            )

        self.assertEqual(status["state"], "SIGNING")
        self.assertEqual(status["lease_owner"], "worker-other")
        self.assertEqual(events, [])

    def test_signed_evidence_without_worker_artifact_is_not_rebroadcast(self):
        self.submit()
        self.set_execution_fields(
            state="SIGNED",
            resource_reservation_id="resource-1",
            signed_raw_tx_ref="not-retained:signed-tron-tx-sha256:abc",
            signed_raw_tx_hash="abc",
            txid="tx-1",
        )
        events = []

        status = self.store_module.PayoutExecutionStore.execute(
            "1",
            wallet=BoundaryWallet(events, self.row),
            resource_ensurer=lambda *args, **kwargs: events.append(("resource",)),
            lease_owner="worker-2",
        )

        self.assertEqual(status["state"], "RECONCILIATION_REQUIRED")
        self.assertEqual(status["failure_class"], "AMBIGUOUS")
        self.assertEqual(events, [])
        self.assertTrue(status["reconciliation_required"])

    def test_cas_conflict_returns_current_state_without_downgrading_terminal(self):
        self.submit()
        self.set_execution_fields(
            state="BROADCASTED",
            resource_reservation_id="resource-1",
            signed_raw_tx_ref="not-retained:signed-tron-tx-sha256:abc",
            signed_raw_tx_hash="abc",
            txid="tx-1",
            txids_json='["tx-1"]',
        )

        exc = self.store_module.PayoutExecutionError(
            "Payout execution state changed concurrently",
            code="PAYOUT_EXECUTION_CAS_CONFLICT",
            status_code=409,
        )
        status = self.store_module.PayoutExecutionStore._mark_failed_or_reconciliation(
            "1",
            exc,
        )

        self.assertEqual(status["state"], "BROADCASTED")
        self.assertEqual(status["txid"], "tx-1")
        self.assertFalse(status["reconciliation_required"])

    def test_failed_handler_does_not_downgrade_broadcasted_after_worker_race(self):
        self.submit()
        self.set_execution_fields(
            state="BROADCASTED",
            resource_reservation_id="resource-1",
            signed_raw_tx_ref="not-retained:signed-tron-tx-sha256:abc",
            signed_raw_tx_hash="abc",
            txid="tx-1",
            txids_json='["tx-1"]',
        )

        status = self.store_module.PayoutExecutionStore._mark_failed_or_reconciliation(
            "1",
            RuntimeError("stale worker"),
        )

        self.assertEqual(status["state"], "BROADCASTED")
        self.assertEqual(status["txid"], "tx-1")
        self.assertFalse(status["reconciliation_required"])

    def test_status_returns_current_row_when_refresh_hits_cas_conflict(self):
        self.submit()
        self.set_execution_fields(state="BROADCASTED", txid="tx-1")
        exc = self.store_module.PayoutExecutionError(
            "Payout execution state changed concurrently",
            code="PAYOUT_EXECUTION_CAS_CONFLICT",
            status_code=409,
        )

        with patch.object(
            self.store_module.PayoutExecutionStore,
            "_refresh_chain_status",
            side_effect=exc,
        ):
            status = self.store_module.PayoutExecutionStore.status(
                "1",
                authenticated_consumer="grither-pay",
            )

        self.assertEqual(status["state"], "BROADCASTED")
        self.assertEqual(status["txid"], "tx-1")

    def test_status_returns_current_row_when_stale_recovery_hits_cas_conflict(self):
        self.submit()
        self.set_execution_fields(
            state="SIGNING",
            lease_expires_at="2026-01-01T00:00:00.000000Z",
        )
        store = self.store_module.PayoutExecutionStore
        exc = self.store_module.PayoutExecutionError(
            "Payout execution state changed concurrently",
            code="PAYOUT_EXECUTION_CAS_CONFLICT",
            status_code=409,
        )

        def racing_recovery(execution_id):
            self.set_execution_fields(
                state="RECEIVED",
                lease_owner=None,
                lease_expires_at=None,
                attempt_id=None,
            )
            raise exc

        with patch.object(store, "recover_stale_execution", side_effect=racing_recovery):
            status = store.status("1", authenticated_consumer="grither-pay")

        self.assertEqual(status["state"], "RECEIVED")
        self.assertEqual(status["lease_owner"], None)

    def test_submit_auto_enqueue_uses_configured_queue_when_enabled(self):
        from app.config import config

        config.PAYOUT_EXECUTION_AUTO_ENQUEUE_ENABLED = True
        calls = []

        with patch.object(
            self.store_module.PayoutExecutionStore,
            "enqueue_execution",
            side_effect=lambda execution_id, queue: calls.append((execution_id, queue)),
        ):
            response = self.submit(_mock_enqueue=False)

        self.assertEqual(response["state"], "RECEIVED")
        self.assertEqual(calls, [("1", "tron_usdt_fee_payouts")])

    def test_submit_rejects_when_auto_enqueue_disabled(self):
        from app.config import config

        config.PAYOUT_EXECUTION_AUTO_ENQUEUE_ENABLED = False

        with self.assertRaisesRegex(
            self.store_module.PayoutExecutionError,
            "auto-enqueue is disabled",
        ) as ctx:
            self.submit()

        self.assertEqual(ctx.exception.code, "PAYOUT_EXECUTION_AUTO_ENQUEUE_DISABLED")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIsNone(self.row())

    def test_duplicate_submit_auto_reenqueues_safe_existing_execution(self):
        from app.config import config

        config.PAYOUT_EXECUTION_AUTO_ENQUEUE_ENABLED = True
        calls = []

        with patch.object(
            self.store_module.PayoutExecutionStore,
            "enqueue_execution",
            side_effect=lambda execution_id, queue: calls.append((execution_id, queue)),
        ):
            first = self.submit(_mock_enqueue=False)
            second = self.submit(_mock_enqueue=False)

        self.assertEqual(first["state"], "RECEIVED")
        self.assertEqual(second["state"], "RECEIVED")
        self.assertEqual(
            calls,
            [
                ("1", "tron_usdt_fee_payouts"),
                ("1", "tron_usdt_fee_payouts"),
            ],
        )

    def test_duplicate_submit_recovers_and_reenqueues_expired_safe_signing(self):
        from app.config import config

        calls = []
        self.submit()
        self.set_execution_fields(
            state="SIGNING",
            lease_owner="dead-worker",
            lease_expires_at="2026-01-01T00:00:00.000000Z",
        )
        config.PAYOUT_EXECUTION_AUTO_ENQUEUE_ENABLED = True

        with patch.object(
            self.store_module.PayoutExecutionStore,
            "enqueue_execution",
            side_effect=lambda execution_id, queue: calls.append((execution_id, queue)),
        ):
            response = self.submit(_mock_enqueue=False)

        self.assertEqual(response["state"], "RECEIVED")
        self.assertEqual(response["lease_owner"], None)
        self.assertEqual(calls, [("1", "tron_usdt_fee_payouts")])

    def test_submit_rejects_mismatched_sidecar_payload_hash(self):
        payload = self.body()
        payload["sidecar_payload_hash"] = "bad-hash"

        with self.assertRaises(self.store_module.PayoutExecutionError):
            self.store_module.PayoutExecutionStore.submit(
                payload,
                authenticated_consumer="grither-pay",
            )

    def test_submit_rejects_wrong_rail_body(self):
        with self.assertRaises(self.store_module.PayoutExecutionError):
            self.submit(network="ETH")


if __name__ == "__main__":
    unittest.main()

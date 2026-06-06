from __future__ import annotations

from decimal import Decimal
import hashlib
import importlib
import json
import os
import sys
import unittest
from unittest.mock import patch

from flask import Flask


TEST_DATABASE = "/private/tmp/tron-shkeeper-payout-status-confirmation.db"
TEST_BALANCES_DATABASE = "/private/tmp/tron-shkeeper-payout-status-confirmation-balances.db"
DESTINATION = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
FEE_DEPOSIT = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"


def reset_database():
    if os.path.exists(TEST_DATABASE):
        os.unlink(TEST_DATABASE)
    if os.path.exists(TEST_BALANCES_DATABASE):
        os.unlink(TEST_BALANCES_DATABASE)

    from app.config import config

    config.DATABASE = TEST_DATABASE
    config.BALANCES_DATABASE = TEST_BALANCES_DATABASE
    config.PAYOUT_EXECUTION_AUTO_ENQUEUE_ENABLED = True
    config.PAYOUT_EXECUTION_PREFLIGHT_CHECKS_ENABLED = True
    config.TRON_USDT_PAYOUT_TX_EXPIRATION_CAP_SEC = 600
    config.TRON_USDT_PAYOUT_MIN_CONFIRMATIONS = 1
    for module_name in [
        "app.payout_execution",
        "app.payout_status",
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


class FakeWallet:
    def __init__(self, _symbol, balance=Decimal("100")):
        self.balance = balance


class FakeQuote:
    def __init__(self, submit_ready=True, code=None, reason=None):
        self.submit_ready = submit_ready
        self.blocking_code = code
        self.blocking_reason = reason

    def to_dict(self):
        return {
            "submit_ready": self.submit_ready,
            "blocking_code": self.blocking_code,
            "blocking_reason": self.blocking_reason,
        }


class FakeClient:
    def __init__(self, tx_block=100, latest_block=105):
        self.tx_block = tx_block
        self.latest_block = latest_block

    def get_transaction(self, txid):
        return {
            "txID": txid,
            "ret": [{"contractRet": "SUCCESS"}],
            "raw_data": {"contract": [{"type": "TriggerSmartContract"}]},
        }

    def get_transaction_info(self, _txid):
        return {
            "receipt": {"result": "SUCCESS"},
            "log": [],
            "blockNumber": self.tx_block,
        }

    def get_latest_block_number(self):
        return self.latest_block


class ExpirationRecordingWallet:
    client = "tron-client"

    def __init__(self):
        self.expiration_ms = None

    def build_signed_transfer(self, destination, amount, *, expiration_ms=None):
        self.expiration_ms = expiration_ms
        return FakeSignedTx()

    def signed_transfer_evidence(self, tx):
        return {
            "signed_raw_tx_ref": "not-retained:signed-tron-tx-sha256:hash",
            "signed_raw_tx_hash": "hash",
            "txid": tx.txid,
            "reference_block": "ref",
            "expiration_at": "2026-06-03T00:10:00.000000Z",
            "chain_check_metadata": {
                "signed_tx_artifact_retention": "NOT_RETAINED_SPENDABLE_RAW_TX",
            },
        }

    def broadcast_signed_transfer(self, tx):
        return {"txid": tx.txid, "receipt": {"result": "SUCCESS"}}


class FakeSignedTx:
    txid = "tx-1"


class PayoutStatusConfirmationTests(unittest.TestCase):
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

    def submit(self, **overrides):
        with patch.object(
            self.store_module.PayoutExecutionStore,
            "enqueue_execution",
            return_value=None,
        ):
            return self.store_module.PayoutExecutionStore.submit(
                self.body(**overrides),
                authenticated_consumer="grither-pay",
            )

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

    def preflight_with_runtime(self, *, wallet_balance=Decimal("100"), quote=None, worker_ready=True):
        payout_status = importlib.import_module("app.payout_status")
        quote = quote or FakeQuote()
        with patch.object(payout_status, "Wallet", lambda symbol: FakeWallet(symbol, wallet_balance)):
            with patch.object(
                payout_status,
                "estimate_fee_deposit_resources_for_usdt_payout",
                lambda destination, amount: quote,
            ):
                with patch.object(
                    payout_status,
                    "usdt_payout_worker_ready",
                    lambda: worker_ready,
                ):
                    return self.store_module.PayoutExecutionStore.preflight(
                        self.body(),
                        authenticated_consumer="grither-pay",
                    )

    def test_preflight_rejects_invalid_address(self):
        with self.assertRaises(self.store_module.PayoutExecutionError) as ctx:
            self.store_module.PayoutExecutionStore.preflight(
                self.body(destination="bad-address"),
                authenticated_consumer="grither-pay",
            )

        self.assertEqual(ctx.exception.code, "INVALID_DESTINATION")

    def test_preflight_rejects_insufficient_usdt(self):
        with self.assertRaises(self.store_module.PayoutExecutionError) as ctx:
            self.preflight_with_runtime(wallet_balance=Decimal("1"))

        self.assertEqual(ctx.exception.code, "INSUFFICIENT_USDT")

    def test_preflight_rejects_unactivated_destination(self):
        quote = FakeQuote(
            submit_ready=False,
            code="DESTINATION_NOT_ACTIVATED",
            reason="TRON payout destination is not activated",
        )

        with self.assertRaises(self.store_module.PayoutExecutionError) as ctx:
            self.preflight_with_runtime(quote=quote)

        self.assertEqual(ctx.exception.code, "DESTINATION_NOT_ACTIVATED")

    def test_preflight_rejects_provider_unreadiness(self):
        quote = FakeQuote(
            submit_ready=False,
            code="PROVIDER_UNAVAILABLE",
            reason="No energy provider is configured",
        )

        with self.assertRaises(self.store_module.PayoutExecutionError) as ctx:
            self.preflight_with_runtime(quote=quote)

        self.assertEqual(ctx.exception.code, "PROVIDER_UNAVAILABLE")

    def test_preflight_rejects_missing_payout_worker(self):
        with self.assertRaises(self.store_module.PayoutExecutionError) as ctx:
            self.preflight_with_runtime(worker_ready=False)

        self.assertEqual(ctx.exception.code, "PAYOUT_WORKER_UNAVAILABLE")

    def test_execution_uses_configured_payout_expiration_cap(self):
        wallet = ExpirationRecordingWallet()
        self.submit()

        self.store_module.PayoutExecutionStore.execute(
            "1",
            wallet=wallet,
            resource_ensurer=lambda *args, **kwargs: None,
            lease_owner="worker-1",
        )

        self.assertEqual(wallet.expiration_ms, 600_000)

    def test_confirmed_transaction_without_transfer_fails_terminally(self):
        self.submit()
        self.set_execution_fields(state="BROADCASTED", txid="tx-1")
        payout_status = importlib.import_module("app.payout_status")

        with patch.object(payout_status.ConnectionManager, "client", lambda: FakeClient()):
            with patch.object(payout_status, "source_wallet_address", lambda ref: FEE_DEPOSIT):
                with patch.object(payout_status, "parse_tx", lambda tx, info: []):
                    status = self.store_module.PayoutExecutionStore.status(
                        "1",
                        authenticated_consumer="grither-pay",
                    )

        self.assertEqual(status["state"], "FAILED_CHAIN_TERMINAL")
        self.assertEqual(status["error_code"], "TRON_USDT_TRANSFER_NOT_FOUND")
        self.assertFalse(status["chain_check_metadata"]["transfer_match"])

    def test_status_chain_check_error_stays_pollable(self):
        self.submit()
        self.set_execution_fields(state="BROADCASTED", txid="tx-1")
        payout_status = importlib.import_module("app.payout_status")

        with patch.object(
            payout_status.ConnectionManager,
            "client",
            side_effect=RuntimeError("node unavailable"),
        ):
            status = self.store_module.PayoutExecutionStore.status(
                "1",
                authenticated_consumer="grither-pay",
            )

        self.assertEqual(status["state"], "CONFIRMING")
        self.assertEqual(
            status["chain_check_metadata"]["confirmation_check"],
            "TRON_USDT_TRC20_TRANSFER",
        )
        self.assertEqual(
            status["chain_check_metadata"]["error"],
            "node unavailable",
        )

    def test_matching_trc20_transfer_confirms_execution(self):
        from app.schemas import TronTransaction

        self.submit()
        self.set_execution_fields(state="BROADCASTED", txid="tx-1")
        payout_status = importlib.import_module("app.payout_status")
        transfer = TronTransaction(
            status="SUCCESS",
            txid="tx-1",
            symbol="USDT",
            src_addr=FEE_DEPOSIT,
            dst_addr=DESTINATION,
            amount=Decimal("25.000000"),
            is_trc20=True,
        )

        with patch.object(payout_status.ConnectionManager, "client", lambda: FakeClient()):
            with patch.object(payout_status, "source_wallet_address", lambda ref: FEE_DEPOSIT):
                with patch.object(payout_status, "parse_tx", lambda tx, info: [transfer]):
                    status = self.store_module.PayoutExecutionStore.status(
                        "1",
                        authenticated_consumer="grither-pay",
                    )

        self.assertEqual(status["state"], "CONFIRMED")
        self.assertTrue(status["chain_check_metadata"]["transfer_match"])
        self.assertGreaterEqual(status["chain_check_metadata"]["confirmations"], 1)
        self.assertEqual(status["txids"], ["tx-1"])

    def test_confirmation_uses_persisted_source_address_when_fee_key_rotates(self):
        from app.schemas import TronTransaction

        self.submit()
        self.set_execution_fields(
            state="BROADCASTED",
            txid="tx-1",
            chain_check_metadata=f'{{"source_wallet_address":"{FEE_DEPOSIT}"}}',
        )
        payout_status = importlib.import_module("app.payout_status")
        transfer = TronTransaction(
            status="SUCCESS",
            txid="tx-1",
            symbol="USDT",
            src_addr=FEE_DEPOSIT,
            dst_addr=DESTINATION,
            amount=Decimal("25.000000"),
            is_trc20=True,
        )

        with patch.object(payout_status.ConnectionManager, "client", lambda: FakeClient()):
            with patch.object(
                payout_status,
                "source_wallet_address",
                lambda ref: DESTINATION,
            ):
                with patch.object(payout_status, "parse_tx", lambda tx, info: [transfer]):
                    status = self.store_module.PayoutExecutionStore.status(
                        "1",
                        authenticated_consumer="grither-pay",
                    )

        self.assertEqual(status["state"], "CONFIRMED")
        self.assertTrue(status["chain_check_metadata"]["transfer_match"])
        self.assertEqual(
            status["chain_check_metadata"]["expected_source"],
            FEE_DEPOSIT,
        )

    def test_matching_trc20_transfer_waits_for_min_confirmations(self):
        from app.config import config
        from app.schemas import TronTransaction

        config.TRON_USDT_PAYOUT_MIN_CONFIRMATIONS = 5
        self.submit()
        self.set_execution_fields(state="BROADCASTED", txid="tx-1")
        payout_status = importlib.import_module("app.payout_status")
        transfer = TronTransaction(
            status="SUCCESS",
            txid="tx-1",
            symbol="USDT",
            src_addr=FEE_DEPOSIT,
            dst_addr=DESTINATION,
            amount=Decimal("25.000000"),
            is_trc20=True,
        )

        with patch.object(
            payout_status.ConnectionManager,
            "client",
            lambda: FakeClient(tx_block=100, latest_block=102),
        ):
            with patch.object(payout_status, "source_wallet_address", lambda ref: FEE_DEPOSIT):
                with patch.object(payout_status, "parse_tx", lambda tx, info: [transfer]):
                    status = self.store_module.PayoutExecutionStore.status(
                        "1",
                        authenticated_consumer="grither-pay",
                    )

        self.assertEqual(status["state"], "CONFIRMING")
        self.assertTrue(status["chain_check_metadata"]["transfer_match"])
        self.assertEqual(status["chain_check_metadata"]["confirmations"], 3)
        self.assertEqual(status["chain_check_metadata"]["min_confirmations"], 5)


if __name__ == "__main__":
    unittest.main()

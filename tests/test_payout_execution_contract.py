from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import sqlite3
import sys
import time
import unittest
from decimal import Decimal
from unittest.mock import patch

import prometheus_client
from flask import Flask


TEST_DATABASE = "/private/tmp/tron-shkeeper-payout-execution-contract.db"
TEST_BALANCES_DATABASE = "/private/tmp/tron-shkeeper-payout-execution-contract-balances.db"
DESTINATION = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"


def reset_database():
    if os.path.exists(TEST_DATABASE):
        os.unlink(TEST_DATABASE)
    if os.path.exists(TEST_BALANCES_DATABASE):
        os.unlink(TEST_BALANCES_DATABASE)

    from app.config import config

    config.DATABASE = TEST_DATABASE
    config.BALANCES_DATABASE = TEST_BALANCES_DATABASE
    config.PAYOUT_CONSUMER_KEYS = {
        "grither-pay": {
            "test-key": {
                "secret": "secret",
                "rails": ["TRON-USDT"],
            }
        },
        "other-consumer": {
            "test-key": {
                "secret": "other-secret",
                "rails": ["TRON-USDT"],
            }
        },
    }
    config.PAYOUT_AUTH_MAX_AGE_SECONDS = 300
    config.PAYOUT_EXECUTION_PREFLIGHT_CHECKS_ENABLED = False
    for module_name in [
        "app.api.payout",
        "app.api",
        "app.payout_auth",
        "app.payout_execution",
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


def payload_hash(payload):
    return sha256_hex(compact_json(payload))


def signature_base(timestamp, nonce, method, path, query, body):
    return "\n".join(
        [
            str(timestamp),
            nonce,
            method.upper(),
            path,
            query,
            sha256_hex(body),
        ]
    )


def sign(secret, base):
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()


class PayoutExecutionContractTests(unittest.TestCase):
    def setUp(self):
        reset_database()
        self.app = Flask(__name__, root_path=os.path.join(os.getcwd(), "app"))
        self.app.config.update(
            TESTING=True,
            DATABASE=TEST_DATABASE,
            API_USERNAME="shkeeper",
            API_PASSWORD="shkeeper",
        )
        self.app.config.DATABASE = TEST_DATABASE
        self.app.config.BALANCES_DATABASE = TEST_BALANCES_DATABASE

        from app import db
        from app import utils

        db.init_app(self.app)
        self.app.url_map.converters["decimal"] = utils.DecimalConverter
        api_module = importlib.import_module("app.api")
        self.app.register_blueprint(api_module.api)
        self.client = self.app.test_client()
        from app.payout_observability import clear_payout_request_metrics

        clear_payout_request_metrics()

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
        payload["sidecar_payload_hash"] = payload_hash(
            canonical_sidecar_payload(payload)
        )
        return payload

    def signed_headers(
        self,
        method,
        path,
        body=b"",
        nonce="nonce-1",
        consumer="grither-pay",
        secret="secret",
    ):
        timestamp = int(time.time())
        base = signature_base(timestamp, nonce, method, path, "", body)
        return {
            "X-Payout-Consumer": consumer,
            "X-Payout-Key-Id": "test-key",
            "X-Payout-Timestamp": str(timestamp),
            "X-Payout-Nonce": nonce,
            "X-Payout-Signature": sign(secret, base),
        }

    def post_json(self, path, payload, nonce="nonce-1"):
        body = compact_json(payload).encode()
        return self.client.post(
            path,
            data=body,
            headers=self.signed_headers("POST", path, body, nonce=nonce),
            content_type="application/json",
            auth=("shkeeper", "shkeeper"),
        )

    def get_signed(self, path, nonce="nonce-status"):
        return self.client.get(
            path,
            headers=self.signed_headers("GET", path, b"", nonce=nonce),
            auth=("shkeeper", "shkeeper"),
        )

    def test_missing_hmac_auth_is_rejected(self):
        response = self.client.post(
            "/USDT/payout/preflight",
            json=self.body(),
            auth=("shkeeper", "shkeeper"),
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["code"], "PAYOUT_AUTH_MISSING")
        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_request_failed_total{code="PAYOUT_AUTH_MISSING",operation="preflight"} 1.0',
            text,
        )

    def test_tampered_body_is_rejected(self):
        payload = self.body()
        body = compact_json(payload).encode()
        tampered_body = compact_json({**payload, "amount": "26"}).encode()

        response = self.client.post(
            "/USDT/payout/preflight",
            data=tampered_body,
            headers=self.signed_headers("POST", "/USDT/payout/preflight", body),
            content_type="application/json",
            auth=("shkeeper", "shkeeper"),
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["code"], "PAYOUT_AUTH_INVALID")

    def test_replayed_nonce_is_rejected(self):
        first = self.post_json("/USDT/payout/preflight", self.body(), nonce="nonce-1")
        second = self.post_json("/USDT/payout/preflight", self.body(), nonce="nonce-1")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 401)
        self.assertEqual(second.get_json()["code"], "PAYOUT_AUTH_REPLAY")

    def test_expired_nonce_is_cleaned_before_reuse(self):
        with self.app.app_context():
            from app.db import get_db

            db = get_db()
            db.execute(
                """
                INSERT INTO payout_auth_nonces
                    (consumer, key_id, nonce, timestamp, created_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                """,
                (
                    "grither-pay",
                    "test-key",
                    "reused-after-expiry",
                    int(time.time()) - 600,
                ),
            )
            db.commit()

        response = self.post_json(
            "/USDT/payout/preflight",
            self.body(),
            nonce="reused-after-expiry",
        )

        self.assertEqual(response.status_code, 200)

    def test_wrong_consumer_is_rejected(self):
        payload = self.body(consumer="other-consumer")

        response = self.post_json("/USDT/payout/preflight", payload)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "PAYOUT_CONSUMER_MISMATCH")

    def test_non_finite_amounts_are_rejected_cleanly(self):
        for raw_amount in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(raw_amount=raw_amount):
                payload = self.body()
                payload["amount"] = raw_amount
                payload["sidecar_payload_hash"] = "not-used-for-invalid-amount"

                response = self.post_json(
                    "/USDT/payout/preflight", payload, nonce=raw_amount
                )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["code"], "INVALID_AMOUNT")

    def test_wrong_rail_body_is_rejected(self):
        payload = self.body(network="ETH")

        response = self.post_json("/USDT/payout/preflight", payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "PAYOUT_RAIL_MISMATCH")
        text = prometheus_client.generate_latest().decode()
        self.assertIn(
            'tron_payout_request_failed_total{code="PAYOUT_RAIL_MISMATCH",operation="preflight"} 1.0',
            text,
        )

    def test_wrong_rail_path_symbol_is_rejected(self):
        payload = self.body()

        response = self.post_json("/TRX/payout/preflight", payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "PAYOUT_RAIL_MISMATCH")

    def test_forbidden_rail_key_is_rejected(self):
        from app.config import config

        config.PAYOUT_CONSUMER_KEYS = {
            "grither-pay": {
                "test-key": {
                    "secret": "secret",
                    "rails": ["TON-USDT"],
                }
            }
        }

        response = self.post_json(
            "/USDT/payout/preflight",
            self.body(),
            nonce="nonce-forbidden-rail",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "PAYOUT_AUTH_FORBIDDEN_RAIL")

    def test_source_wallet_override_is_rejected(self):
        response = self.post_json(
            "/USDT/payout/preflight",
            self.body(source_wallet_ref="other_wallet"),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "PAYOUT_SOURCE_WALLET_MISMATCH")

    def test_payout_queue_override_is_rejected(self):
        response = self.post_json(
            "/USDT/payout/preflight",
            self.body(payout_queue="other_queue"),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "PAYOUT_QUEUE_MISMATCH")

    def test_mismatched_sidecar_payload_hash_is_rejected(self):
        payload = self.body()
        payload["sidecar_payload_hash"] = "bad-hash"

        response = self.post_json("/USDT/payout/preflight", payload)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "SIDECAR_PAYLOAD_HASH_MISMATCH")

    def test_unknown_execution_fields_are_rejected(self):
        value = self.body(
            unsupported_alpha="value-1",
            unsupported_beta="value-2",
        )

        response = self.post_json("/USDT/payout/submit", value)

        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertEqual(data["code"], "PAYOUT_EXECUTION_BAD_REQUEST")
        self.assertIn("unsupported_alpha", data["message"])
        self.assertIn("unsupported_beta", data["message"])

    def test_preflight_accepts_valid_payload(self):
        response = self.post_json("/USDT/payout/preflight", self.body())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "OK")

    def test_submit_is_idempotent_for_same_payload(self):
        payload = self.body()

        first = self.post_json("/USDT/payout/submit", payload, nonce="nonce-1")
        second = self.post_json("/USDT/payout/submit", payload, nonce="nonce-2")

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.get_json()["execution_id"], "1")
        self.assertEqual(second.get_json()["execution_id"], "1")
        self.assertEqual(first.get_json()["request_hash"], "request-hash")
        self.assertEqual(first.get_json()["state"], "RECEIVED")
        self.assertEqual(first.get_json()["sidecar_state"], "RECEIVED")
        self.assertEqual(first.get_json()["state_version"], 1)
        self.assertTrue(first.get_json()["state_transition_id"])
        self.assertEqual(first.get_json()["source_wallet_ref"], "fee_deposit")
        self.assertEqual(
            first.get_json()["token_contract"],
            "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        )

    def test_consumer_keys_json_accepts_parsed_dict(self):
        from app import payout_auth
        from app.config import config

        config.PAYOUT_CONSUMER_KEYS = None
        config.PAYOUT_CONSUMER_KEYS_JSON = None
        self.app.config["PAYOUT_CONSUMER_KEYS"] = None
        self.app.config["PAYOUT_CONSUMER_KEYS_JSON"] = {
            "grither-pay": {
                "test-key": {
                    "secret": "secret",
                    "rails": ["TRON-USDT"],
                }
            }
        }

        payload = self.body()
        body = compact_json(payload).encode()
        headers = self.signed_headers(
            "POST",
            "/USDT/payout/preflight",
            body,
            nonce="nonce-json-dict",
        )
        with patch.object(payout_auth, "_remember_nonce", return_value=True):
            response = self.client.post(
                "/USDT/payout/preflight",
                data=body,
                headers=headers,
                content_type="application/json",
                auth=("shkeeper", "shkeeper"),
            )

        self.assertEqual(response.status_code, 200)

    def test_submit_race_on_unique_constraint_returns_existing_execution(self):
        from app import payout_execution

        class Cursor:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        existing_row = {
            "execution_id": "1",
            "consumer": "grither-pay",
            "external_id": "WD-1",
            "request_hash": "request-hash",
            "sidecar_payload_hash": self.body()["sidecar_payload_hash"],
            "state": "RECEIVED",
            "state_version": 1,
            "state_transition_id": "transition-id",
            "state_updated_at": "2026-06-03T00:00:00Z",
            "source_wallet": "fee_deposit",
            "token_contract": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            "chain_id_or_network_id": "mainnet",
            "payout_queue": "tron_usdt_fee_payouts",
            "lease_owner": None,
            "lease_expires_at": None,
            "attempt_id": None,
            "resource_reservation_id": None,
            "reference_block": None,
            "expiration_at": None,
            "signed_raw_tx_ref": None,
            "signed_raw_tx_hash": None,
            "signed_raw_tx_stored_at": None,
            "txid": None,
            "broadcast_provider": None,
            "broadcast_attempted_at": None,
            "chain_check_metadata": None,
            "failure_class": None,
            "error_code": None,
            "error_message": None,
            "reconciliation_required": 0,
            "txids_json": "[]",
            "message_hashes_json": "[]",
            "canonical_payload_json": compact_json(canonical_sidecar_payload(self.body())),
        }

        class RaceDb:
            def execute(self, query, args=()):
                if "INSERT INTO payout_executions" in query:
                    raise sqlite3.IntegrityError("race")
                if "WHERE execution_id = ? OR" in query:
                    return Cursor(existing_row)
                return Cursor(None)

            def commit(self):
                pass

            def rollback(self):
                pass

        with patch.object(payout_execution, "get_db", return_value=RaceDb()):
            response = payout_execution.PayoutExecutionStore.submit(
                self.body(),
                authenticated_consumer="grither-pay",
            )

        self.assertEqual(response["status"], "ACCEPTED")
        self.assertEqual(response["execution_id"], "1")

    def test_submit_rejects_duplicate_changed_payload(self):
        first = self.post_json("/USDT/payout/submit", self.body(), nonce="nonce-1")
        changed = self.body(amount="26")
        second = self.post_json("/USDT/payout/submit", changed, nonce="nonce-2")

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()["code"], "PAYOUT_EXECUTION_CONFLICT")

    def test_status_returns_execution_by_id(self):
        self.post_json("/USDT/payout/submit", self.body(), nonce="nonce-submit")

        response = self.get_signed("/USDT/payout/status/1")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["status"], "OK")
        self.assertEqual(data["execution_id"], "1")
        self.assertEqual(data["sidecar_execution_id"], "1")
        self.assertEqual(data["external_id"], "WD-1")
        self.assertEqual(data["contract_version"], "usdt-payout-execution-v1")
        self.assertEqual(data["asset"], "USDT")
        self.assertEqual(data["network"], "TRON")
        self.assertEqual(data["amount"], "25.000000")
        self.assertEqual(data["destination"], DESTINATION)
        self.assertEqual(data["state"], "RECEIVED")
        self.assertEqual(data["sidecar_payload_hash"], self.body()["sidecar_payload_hash"])

    def test_status_is_scoped_to_authenticated_consumer(self):
        self.post_json("/USDT/payout/submit", self.body(), nonce="nonce-submit")
        path = "/USDT/payout/status/1"

        response = self.client.get(
            path,
            headers=self.signed_headers(
                "GET",
                path,
                b"",
                nonce="nonce-other-status",
                consumer="other-consumer",
                secret="other-secret",
            ),
            auth=("shkeeper", "shkeeper"),
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], "NO_EXECUTION_CREATED")

    def test_shkeeper_v1_routes_are_supported(self):
        payload = self.body()

        preflight = self.post_json(
            "/USDT/payout-executions/1/preflight",
            payload,
            nonce="nonce-preflight",
        )
        submit = self.post_json(
            "/USDT/payout-executions/1",
            payload,
            nonce="nonce-submit",
        )
        status = self.get_signed("/USDT/payout-executions/1")

        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(submit.status_code, 202)
        self.assertEqual(status.status_code, 200)

    def test_shkeeper_v1_routes_do_not_require_legacy_basic_auth(self):
        payload = self.body()
        body = compact_json(payload).encode()

        preflight_path = "/USDT/payout-executions/1/preflight"
        preflight = self.client.post(
            preflight_path,
            data=body,
            headers=self.signed_headers(
                "POST",
                preflight_path,
                body,
                nonce="nonce-v1-preflight-no-basic",
            ),
            content_type="application/json",
        )
        submit_path = "/USDT/payout-executions/1"
        submit = self.client.post(
            submit_path,
            data=body,
            headers=self.signed_headers(
                "POST",
                submit_path,
                body,
                nonce="nonce-v1-submit-no-basic",
            ),
            content_type="application/json",
        )
        status = self.client.get(
            submit_path,
            headers=self.signed_headers(
                "GET",
                submit_path,
                b"",
                nonce="nonce-v1-status-no-basic",
            ),
        )

        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(submit.status_code, 202)
        self.assertEqual(status.status_code, 200)

    def test_legacy_multipayout_still_requires_basic_auth(self):
        response = self.client.post(
            "/USDT/multipayout",
            json=[{"dest": "TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7", "amount": "1"}],
        )

        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()

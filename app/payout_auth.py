from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time

from flask import current_app, g, request

from .db import get_db
from .config import config
from .payout_observability import (
    payout_operation_from_request,
    record_payout_request_failed,
)


PAYOUT_CONSUMER_HEADER = "X-Payout-Consumer"
PAYOUT_KEY_ID_HEADER = "X-Payout-Key-Id"
PAYOUT_TIMESTAMP_HEADER = "X-Payout-Timestamp"
PAYOUT_NONCE_HEADER = "X-Payout-Nonce"
PAYOUT_SIGNATURE_HEADER = "X-Payout-Signature"


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def signature_base(timestamp, nonce, method, canonical_path, canonical_query, body):
    return "\n".join(
        [
            str(timestamp),
            nonce,
            method.upper(),
            canonical_path,
            canonical_query,
            sha256_hex(body),
        ]
    )


def sign_request(secret, base):
    return hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()


def _configured_keys():
    keys = getattr(config, "PAYOUT_CONSUMER_KEYS", None)
    if keys:
        return keys
    keys = current_app.config.get("PAYOUT_CONSUMER_KEYS")
    if keys:
        return keys
    raw = (
        current_app.config.get("PAYOUT_CONSUMER_KEYS_JSON")
        or getattr(config, "PAYOUT_CONSUMER_KEYS_JSON", None)
    )
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    return json.loads(raw)


def get_consumer_key_config(consumer, key_id):
    consumer_keys = _configured_keys().get(consumer, {})
    if isinstance(consumer_keys, str):
        return {"secret": consumer_keys} if key_id == "default" else None
    key_config = consumer_keys.get(key_id)
    if isinstance(key_config, str):
        return {"secret": key_config}
    return key_config


def get_consumer_secret(consumer, key_id):
    key_config = get_consumer_key_config(consumer, key_id)
    if not key_config:
        return None
    return key_config.get("secret")


def key_allows_rail(consumer, key_id, rail):
    key_config = get_consumer_key_config(consumer, key_id)
    if not key_config:
        return False
    rails = key_config.get("rails") or key_config.get("allowed_rails")
    if not rails:
        return False
    return rail in rails


def _response(code, message, status_code=401):
    return {"status": "error", "code": code, "message": message}, status_code


def _remember_nonce(consumer, key_id, nonce, timestamp):
    db = get_db()
    try:
        tolerance = int(
            current_app.config.get(
                "PAYOUT_AUTH_MAX_AGE_SECONDS",
                getattr(config, "PAYOUT_AUTH_MAX_AGE_SECONDS", 300),
            )
        )
        db.execute(
            """
            DELETE FROM payout_auth_nonces
            WHERE timestamp < ?
            """,
            (int(time.time()) - tolerance,),
        )
        db.execute(
            """
            INSERT INTO payout_auth_nonces (consumer, key_id, nonce, timestamp, created_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (consumer, key_id, nonce, timestamp),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return False
    return True


def verify_payout_request(body, rail="TRON-USDT"):
    consumer = request.headers.get(PAYOUT_CONSUMER_HEADER, "").strip()
    key_id = request.headers.get(PAYOUT_KEY_ID_HEADER, "").strip()
    timestamp = request.headers.get(PAYOUT_TIMESTAMP_HEADER, "").strip()
    nonce = request.headers.get(PAYOUT_NONCE_HEADER, "").strip()
    signature = request.headers.get(PAYOUT_SIGNATURE_HEADER, "").strip().lower()
    if not all([consumer, key_id, timestamp, nonce, signature]):
        return _response("PAYOUT_AUTH_MISSING", "Missing payout auth headers")
    if len(signature) != 64:
        return _response("PAYOUT_AUTH_INVALID", "Invalid payout signature")
    try:
        timestamp_int = int(timestamp)
    except ValueError:
        return _response("PAYOUT_AUTH_INVALID", "Invalid payout timestamp")

    tolerance = int(
        current_app.config.get(
            "PAYOUT_AUTH_MAX_AGE_SECONDS",
            getattr(config, "PAYOUT_AUTH_MAX_AGE_SECONDS", 300),
        )
    )
    if abs(int(time.time()) - timestamp_int) > tolerance:
        return _response("PAYOUT_AUTH_EXPIRED", "Expired payout auth timestamp")

    secret = get_consumer_secret(consumer, key_id)
    if not secret:
        return _response("PAYOUT_AUTH_UNKNOWN_KEY", "Unknown payout auth key")

    base = signature_base(
        timestamp_int,
        nonce,
        request.method,
        request.path,
        request.query_string.decode("utf-8"),
        body,
    )
    expected = sign_request(secret, base)
    if not hmac.compare_digest(expected, signature):
        return _response("PAYOUT_AUTH_INVALID", "Invalid payout signature")
    if not key_allows_rail(consumer, key_id, rail):
        return _response(
            "PAYOUT_AUTH_FORBIDDEN_RAIL",
            "Payout auth key is not allowed for this rail",
            status_code=403,
        )
    if not _remember_nonce(consumer, key_id, nonce, timestamp_int):
        return _response("PAYOUT_AUTH_REPLAY", "Replayed payout auth nonce")

    g.payout_consumer = consumer
    g.payout_key_id = key_id
    return None


def payout_auth_required(view):
    def wrapped(*args, **kwargs):
        body = request.get_data(cache=True) or b""
        error_response = verify_payout_request(body)
        if error_response is not None:
            error_body, _status_code = error_response
            record_payout_request_failed(
                payout_operation_from_request(request.method, request.path),
                error_body.get("code"),
            )
            return error_response
        return view(*args, **kwargs)

    wrapped.__name__ = view.__name__
    wrapped.__doc__ = view.__doc__
    return wrapped

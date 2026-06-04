from __future__ import annotations

import json
import sqlite3
import uuid

import requests

from .config import config


STATUS_PENDING = "PENDING"
STATUS_DISPATCHING = "DISPATCHING"
STATUS_SENT = "SENT"
STATUS_FAILED = "FAILED"


def _connect():
    db = sqlite3.connect(
        config.DATABASE,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,
    )
    db.execute("pragma journal_mode=wal;")
    db.row_factory = sqlite3.Row
    return db


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _retry_delay_modifier():
    return f"+{int(config.PAYOUT_CALLBACK_RETRY_DELAY_SEC)} seconds"


def _claim_ttl_modifier():
    return f"-{int(config.PAYOUT_CALLBACK_CLAIM_TTL_SEC)} seconds"


def create_payout_callback(data, symbol):
    payload_json = _json(data)
    db = _connect()
    try:
        cursor = db.execute(
            """
            INSERT INTO payout_callback_outbox (
                symbol, payload_json, status, attempts, next_attempt_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, 0, datetime('now'), datetime('now'), datetime('now'))
            """,
            (symbol, payload_json, STATUS_PENDING),
        )
        db.commit()
        return cursor.lastrowid
    finally:
        db.close()


def get_payout_callback(outbox_id):
    db = _connect()
    try:
        row = db.execute(
            """
            SELECT * FROM payout_callback_outbox WHERE id = ?
            """,
            (int(outbox_id),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def claim_payout_callback(outbox_id, claim_token=None):
    claim_token = claim_token or str(uuid.uuid4())
    db = _connect()
    try:
        db.execute(
            """
            UPDATE payout_callback_outbox
            SET
                status = ?,
                claimed_at = datetime('now'),
                claim_token = ?,
                updated_at = datetime('now')
            WHERE id = ?
              AND attempts < ?
              AND (
                (
                  status = ?
                  AND (
                    next_attempt_at IS NULL
                    OR next_attempt_at <= datetime('now')
                  )
                )
                OR (
                  status = ?
                  AND claimed_at <= datetime('now', ?)
                )
              )
            """,
            (
                STATUS_DISPATCHING,
                claim_token,
                int(outbox_id),
                config.PAYOUT_CALLBACK_MAX_ATTEMPTS,
                STATUS_PENDING,
                STATUS_DISPATCHING,
                _claim_ttl_modifier(),
            ),
        )
        db.commit()
        return get_payout_callback(outbox_id)
    finally:
        db.close()


def claim_due_payout_callbacks(limit, claim_token=None):
    claim_token = claim_token or str(uuid.uuid4())
    db = _connect()
    try:
        db.execute(
            """
            UPDATE payout_callback_outbox
            SET
                status = ?,
                claimed_at = datetime('now'),
                claim_token = ?,
                updated_at = datetime('now')
            WHERE id IN (
                SELECT id
                FROM payout_callback_outbox
                WHERE attempts < ?
                  AND (
                    (
                      status = ?
                      AND (
                        next_attempt_at IS NULL
                        OR next_attempt_at <= datetime('now')
                      )
                    )
                    OR (
                      status = ?
                      AND claimed_at <= datetime('now', ?)
                    )
                  )
                ORDER BY COALESCE(next_attempt_at, created_at), id
                LIMIT ?
            )
            """,
            (
                STATUS_DISPATCHING,
                claim_token,
                config.PAYOUT_CALLBACK_MAX_ATTEMPTS,
                STATUS_PENDING,
                STATUS_DISPATCHING,
                _claim_ttl_modifier(),
                int(limit),
            ),
        )
        db.commit()
        rows = db.execute(
            """
            SELECT *
            FROM payout_callback_outbox
            WHERE status = ? AND claim_token = ?
            ORDER BY id
            """,
            (STATUS_DISPATCHING, claim_token),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def _update_after_attempt(
    outbox_id,
    *,
    claim_token,
    status,
    http_status=None,
    response_text=None,
    error=None,
):
    db = _connect()
    try:
        cursor = db.execute(
            """
            UPDATE payout_callback_outbox
            SET
                status = ?,
                attempts = attempts + 1,
                next_attempt_at = CASE
                    WHEN ? = ? THEN datetime('now', ?)
                    ELSE NULL
                END,
                claimed_at = NULL,
                claim_token = NULL,
                last_http_status = ?,
                last_response_text = ?,
                last_error = ?,
                updated_at = datetime('now'),
                sent_at = CASE WHEN ? = ? THEN datetime('now') ELSE sent_at END
            WHERE id = ? AND status = ? AND claim_token = ?
            """,
            (
                status,
                status,
                STATUS_PENDING,
                _retry_delay_modifier(),
                http_status,
                response_text[:1000] if response_text else None,
                error,
                status,
                STATUS_SENT,
                int(outbox_id),
                STATUS_DISPATCHING,
                claim_token,
            ),
        )
        db.commit()
        if cursor.rowcount == 0:
            return get_payout_callback(outbox_id)
        return get_payout_callback(outbox_id)
    finally:
        db.close()


def dispatch_payout_callback(outbox_id, claim_token=None):
    row = get_payout_callback(outbox_id)
    if row is None:
        return {"status": STATUS_FAILED, "error": "callback outbox row not found"}
    if row["status"] == STATUS_SENT:
        return row
    if row["status"] == STATUS_FAILED:
        return row
    if row["status"] == STATUS_DISPATCHING:
        if not claim_token or row["claim_token"] != claim_token:
            return row
    else:
        claim_token = claim_token or str(uuid.uuid4())
        row = claim_payout_callback(outbox_id, claim_token=claim_token)
        if row is None:
            return {"status": STATUS_FAILED, "error": "callback outbox row not found"}
        if row["status"] != STATUS_DISPATCHING or row["claim_token"] != claim_token:
            return row

    payload = json.loads(row["payload_json"])
    try:
        response = requests.post(
            f"http://{config.SHKEEPER_HOST}/api/v1/payoutnotify/{row['symbol']}",
            headers={"X-Shkeeper-Backend-Key": config.SHKEEPER_BACKEND_KEY},
            json=payload,
            timeout=config.PAYOUT_CALLBACK_TIMEOUT_SEC,
        )
    except Exception as exc:
        next_attempts = int(row["attempts"]) + 1
        status = (
            STATUS_FAILED
            if next_attempts >= config.PAYOUT_CALLBACK_MAX_ATTEMPTS
            else STATUS_PENDING
        )
        updated = _update_after_attempt(
            outbox_id,
            claim_token=claim_token,
            status=status,
            error=str(exc),
        )
        return updated

    response_text = getattr(response, "text", "")
    http_status = getattr(response, "status_code", None)
    sent = http_status is not None and 200 <= http_status < 300
    next_attempts = int(row["attempts"]) + 1
    status = (
        STATUS_SENT
        if sent
        else (
            STATUS_FAILED
            if next_attempts >= config.PAYOUT_CALLBACK_MAX_ATTEMPTS
            else STATUS_PENDING
        )
    )
    return _update_after_attempt(
        outbox_id,
        claim_token=claim_token,
        status=status,
        http_status=http_status,
        response_text=response_text,
        error=None if sent else f"HTTP {http_status}",
    )


def should_retry(row):
    return (
        row is not None
        and row["status"] == STATUS_PENDING
        and int(row["attempts"]) < config.PAYOUT_CALLBACK_MAX_ATTEMPTS
    )

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import tronpy

from .config import config
from .db import get_db
from .payout_auth import sha256_hex
from .schemas import TronSymbol


USDT_DECIMALS = 6
CONTRACT_VERSION = "usdt-payout-execution-v1"
STATE_RECEIVED = "RECEIVED"
STATE_VALIDATED = "VALIDATED"
STATE_SIGNING = "SIGNING"
STATE_SIGNED = "SIGNED"
STATE_BROADCASTING = "BROADCASTING"
STATE_BROADCASTED = "BROADCASTED"
STATE_CONFIRMING = "CONFIRMING"
STATE_CONFIRMED = "CONFIRMED"
STATE_FAILED_PRE_BROADCAST = "FAILED_PRE_BROADCAST"
STATE_FAILED_CHAIN_TERMINAL = "FAILED_CHAIN_TERMINAL"
STATE_RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
UNSAFE_RECOVERY_STATES = (STATE_SIGNING, STATE_SIGNED, STATE_BROADCASTING)
NO_DOWNGRADE_STATES = (
    STATE_BROADCASTED,
    STATE_CONFIRMING,
    STATE_CONFIRMED,
    STATE_FAILED_PRE_BROADCAST,
    STATE_FAILED_CHAIN_TERMINAL,
    STATE_RECONCILIATION_REQUIRED,
)


class PayoutExecutionError(ValueError):
    def __init__(self, message, *, code, status_code=400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def compact_json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def hash_payload(payload):
    return sha256_hex(compact_json(payload))


def canonical_amount(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PayoutExecutionError(
            "amount must be a decimal string",
            code="INVALID_AMOUNT",
        ) from exc
    if not amount.is_finite():
        raise PayoutExecutionError(
            "amount must be finite", code="INVALID_AMOUNT"
        )
    if amount <= 0:
        raise PayoutExecutionError("amount must be positive", code="INVALID_AMOUNT")
    if amount.as_tuple().exponent < -USDT_DECIMALS:
        raise PayoutExecutionError(
            "amount supports at most 6 decimal places",
            code="INVALID_AMOUNT_PRECISION",
        )
    return amount.quantize(Decimal("0.000001"))


def canonical_sidecar_payload(payload):
    return {
        "consumer": payload["consumer"],
        "execution_id": str(payload["execution_id"]),
        "external_id": str(payload["external_id"]),
        "asset": payload["asset"].upper(),
        "network": payload["network"].upper(),
        "amount": format(canonical_amount(payload["amount"]), "f"),
        "destination": str(payload["destination"]),
        "contract_version": payload.get("contract_version") or CONTRACT_VERSION,
    }


def _now_expr():
    return "datetime('now')"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _format_datetime(value):
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds") + "Z"


def _parse_datetime(value):
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class PayoutExecutionStore:
    REQUIRED_FIELDS = (
        "consumer",
        "execution_id",
        "external_id",
        "asset",
        "network",
        "amount",
        "destination",
        "contract_version",
        "request_hash",
        "sidecar_payload_hash",
    )
    OPTIONAL_FIELDS = (
        "source_wallet_ref",
        "payout_queue",
    )
    ALLOWED_FIELDS = frozenset(REQUIRED_FIELDS + OPTIONAL_FIELDS)

    @staticmethod
    def validate_endpoint_symbol(endpoint_symbol):
        if str(endpoint_symbol).upper() != "USDT":
            raise PayoutExecutionError(
                "Endpoint symbol does not match TRON USDT payout rail",
                code="PAYOUT_RAIL_MISMATCH",
            )

    @classmethod
    def validate_payload(
        cls,
        payload,
        *,
        authenticated_consumer,
        execution_id=None,
        endpoint_symbol="USDT",
    ):
        cls.validate_endpoint_symbol(endpoint_symbol)
        if not isinstance(payload, dict):
            raise PayoutExecutionError(
                "Payout execution body must be a JSON object",
                code="PAYOUT_EXECUTION_BAD_REQUEST",
            )
        unknown = sorted(set(payload) - cls.ALLOWED_FIELDS)
        if unknown:
            raise PayoutExecutionError(
                "Payout execution request contains unsupported fields: "
                f"{', '.join(unknown)}. TRON sidecar accepts only execution "
                "contract fields.",
                code="PAYOUT_EXECUTION_BAD_REQUEST",
            )
        missing = [field for field in cls.REQUIRED_FIELDS if payload.get(field) is None]
        if missing:
            raise PayoutExecutionError(
                f"Missing payout execution fields: {', '.join(missing)}",
                code="PAYOUT_EXECUTION_BAD_REQUEST",
            )
        if payload["consumer"] != authenticated_consumer:
            raise PayoutExecutionError(
                "Authenticated consumer does not match request body",
                code="PAYOUT_CONSUMER_MISMATCH",
                status_code=403,
            )
        if execution_id is not None and str(payload["execution_id"]) != str(execution_id):
            raise PayoutExecutionError(
                "Execution id in path does not match request body",
                code="PAYOUT_EXECUTION_ID_MISMATCH",
            )
        if payload["asset"].upper() != "USDT" or payload["network"].upper() != "TRON":
            raise PayoutExecutionError(
                "Payout body does not match TRON USDT rail",
                code="PAYOUT_RAIL_MISMATCH",
            )
        source_wallet = payload.get("source_wallet_ref")
        if source_wallet not in (None, "fee_deposit"):
            raise PayoutExecutionError(
                "TRON USDT payouts currently use fee_deposit source wallet",
                code="PAYOUT_SOURCE_WALLET_MISMATCH",
            )
        payout_queue = payload.get("payout_queue")
        if payout_queue not in (None, config.TRON_USDT_PAYOUT_QUEUE):
            raise PayoutExecutionError(
                "TRON USDT payouts must use the configured payout queue",
                code="PAYOUT_QUEUE_MISMATCH",
            )
        try:
            tronpy.keys.to_base58check_address(payload["destination"])
        except Exception as exc:
            raise PayoutExecutionError(
                "Invalid TRON destination address",
                code="INVALID_DESTINATION",
            ) from exc
        canonical = canonical_sidecar_payload(payload)
        expected_hash = hash_payload(canonical)
        if payload["sidecar_payload_hash"] != expected_hash:
            raise PayoutExecutionError(
                "sidecar_payload_hash does not match TRON canonical payload",
                code="SIDECAR_PAYLOAD_HASH_MISMATCH",
                status_code=409,
            )
        return canonical

    @staticmethod
    def _row_to_status(row, status="OK"):
        canonical = json.loads(row["canonical_payload_json"] or "{}")
        return {
            "status": status,
            "execution_id": row["execution_id"],
            "sidecar_execution_id": row["execution_id"],
            "consumer": row["consumer"],
            "external_id": row["external_id"],
            "contract_version": canonical.get("contract_version"),
            "asset": canonical.get("asset"),
            "network": canonical.get("network"),
            "amount": canonical.get("amount"),
            "destination": canonical.get("destination"),
            "request_hash": row["request_hash"],
            "sidecar_payload_hash": row["sidecar_payload_hash"],
            "state": row["state"],
            "sidecar_state": row["state"],
            "state_version": row["state_version"],
            "sidecar_state_version": row["state_version"],
            "state_transition_id": row["state_transition_id"],
            "sidecar_state_transition_id": row["state_transition_id"],
            "state_updated_at": row["state_updated_at"],
            "source_wallet": row["source_wallet"],
            "source_wallet_ref": row["source_wallet"],
            "token_contract": row["token_contract"],
            "chain_id_or_network_id": row["chain_id_or_network_id"],
            "payout_queue": row["payout_queue"],
            "lease_owner": row["lease_owner"],
            "lease_expires_at": row["lease_expires_at"],
            "attempt_id": row["attempt_id"],
            "resource_reservation_id": row["resource_reservation_id"],
            "reference_block": row["reference_block"],
            "expiration_at": row["expiration_at"],
            "signed_raw_tx_ref": row["signed_raw_tx_ref"],
            "signed_raw_tx_hash": row["signed_raw_tx_hash"],
            "signed_raw_tx_stored_at": row["signed_raw_tx_stored_at"],
            "txid": row["txid"],
            "broadcast_provider": row["broadcast_provider"],
            "broadcast_attempted_at": row["broadcast_attempted_at"],
            "chain_check_metadata": json.loads(row["chain_check_metadata"] or "{}"),
            "failure_class": row["failure_class"],
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "reconciliation_required": bool(row["reconciliation_required"]),
            "txids": json.loads(row["txids_json"] or "[]"),
            "message_hashes": json.loads(row["message_hashes_json"] or "[]"),
        }

    @staticmethod
    def _get_row(execution_id):
        return get_db().execute(
            "SELECT * FROM payout_executions WHERE execution_id = ?",
            (str(execution_id),),
        ).fetchone()

    @staticmethod
    def _json(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

    @classmethod
    def _transition(cls, row, state, **fields):
        if row is None:
            raise PayoutExecutionError(
                "Payout execution was not created",
                code="NO_EXECUTION_CREATED",
                status_code=404,
            )
        allowed_fields = {
            "lease_owner",
            "lease_expires_at",
            "attempt_id",
            "resource_reservation_id",
            "reference_block",
            "expiration_at",
            "signed_raw_tx_ref",
            "signed_raw_tx_hash",
            "signed_raw_tx_stored_at",
            "txid",
            "broadcast_provider",
            "broadcast_attempted_at",
            "chain_check_metadata",
            "failure_class",
            "error_code",
            "error_message",
            "reconciliation_required",
            "txids_json",
            "message_hashes_json",
        }
        unknown = set(fields) - allowed_fields
        if unknown:
            raise ValueError(f"Unexpected payout execution fields: {sorted(unknown)}")
        state_transition_id = str(uuid.uuid4())
        assignments = [
            "state = ?",
            "state_version = state_version + 1",
            "state_transition_id = ?",
            f"state_updated_at = {_now_expr()}",
        ]
        values = [state, state_transition_id]
        for field, value in fields.items():
            assignments.append(f"{field} = ?")
            values.append(value)
        values.extend([row["execution_id"], row["state_version"]])
        db = get_db()
        cursor = db.execute(
            f"""
            UPDATE payout_executions
            SET {", ".join(assignments)}
            WHERE execution_id = ? AND state_version = ?
            """,
            tuple(values),
        )
        if cursor.rowcount != 1:
            db.rollback()
            raise PayoutExecutionError(
                "Payout execution state changed concurrently",
                code="PAYOUT_EXECUTION_CAS_CONFLICT",
                status_code=409,
            )
        db.commit()
        return cls._get_row(row["execution_id"])

    @classmethod
    def _transition_by_id(cls, execution_id, state, **fields):
        return cls._transition(cls._get_row(execution_id), state, **fields)

    @staticmethod
    def _has_unsafe_side_effect(row):
        return any(
            row[field]
            for field in (
                "signed_raw_tx_ref",
                "signed_raw_tx_hash",
                "txid",
                "broadcast_attempted_at",
            )
        )

    @staticmethod
    def _lease_expired(row):
        expires_at = _parse_datetime(row["lease_expires_at"])
        if expires_at is None:
            return True
        return expires_at <= _utcnow()

    @classmethod
    def recover_stale_execution(cls, execution_id):
        row = cls._get_row(execution_id)
        if row is None:
            raise PayoutExecutionError(
                "Payout execution was not created",
                code="NO_EXECUTION_CREATED",
                status_code=404,
            )
        if row["state"] not in UNSAFE_RECOVERY_STATES:
            return cls._row_to_status(row)
        if not cls._lease_expired(row):
            return cls._row_to_status(row)

        if row["state"] in (STATE_SIGNED, STATE_BROADCASTING):
            row = cls._transition(
                row,
                STATE_RECONCILIATION_REQUIRED,
                failure_class="AMBIGUOUS",
                error_code=f"STALE_{row['state']}_WITH_SIDE_EFFECT",
                error_message=(
                    f"Stale {row['state']} cannot be automatically retried "
                    "because signing or broadcasting may already have happened"
                ),
                reconciliation_required=1,
            )
            return cls._row_to_status(row)

        if cls._has_unsafe_side_effect(row):
            row = cls._transition(
                row,
                STATE_RECONCILIATION_REQUIRED,
                failure_class="AMBIGUOUS",
                error_code="STALE_SIGNING_WITH_SIDE_EFFECT",
                error_message=(
                    "Stale SIGNING contains resource, signed transaction, or "
                    "broadcast evidence and cannot be automatically retried"
                ),
                reconciliation_required=1,
            )
        else:
            row = cls._transition(
                row,
                STATE_RECEIVED,
                lease_owner=None,
                lease_expires_at=None,
                attempt_id=None,
                reconciliation_required=0,
            )
        return cls._row_to_status(row)

    @classmethod
    def recover_stale_signing(cls, execution_id):
        return cls.recover_stale_execution(execution_id)

    @classmethod
    def _mark_resource_reserved(cls, row, reservation_id):
        return cls._transition(
            row,
            STATE_SIGNING,
            resource_reservation_id=reservation_id,
        )

    @classmethod
    def _mark_signed(cls, row, evidence):
        return cls._transition(
            row,
            STATE_SIGNED,
            signed_raw_tx_ref=evidence["signed_raw_tx_ref"],
            signed_raw_tx_hash=evidence["signed_raw_tx_hash"],
            signed_raw_tx_stored_at=_format_datetime(_utcnow()),
            reference_block=evidence.get("reference_block"),
            expiration_at=evidence.get("expiration_at"),
            txid=evidence["txid"],
            chain_check_metadata=cls._json(evidence.get("chain_check_metadata") or {}),
        )

    @classmethod
    def _mark_broadcasting(cls, row, provider="tronpy"):
        return cls._transition(
            row,
            STATE_BROADCASTING,
            broadcast_provider=provider,
            broadcast_attempted_at=_format_datetime(_utcnow()),
        )

    @staticmethod
    def _normalize_txid(value):
        if value is None:
            return None
        text = str(value).strip()
        return text.lower() if text else None

    @classmethod
    def _broadcast_result_txids(cls, result):
        if not isinstance(result, dict):
            return []
        candidates = []
        for key in ("txid", "txID", "id", "transaction_id", "broadcast_txid"):
            normalized = cls._normalize_txid(result.get(key))
            if normalized and normalized not in candidates:
                candidates.append(normalized)
        return candidates

    @classmethod
    def _verify_broadcast_result(cls, row, expected_txid, result):
        expected = cls._normalize_txid(expected_txid)
        candidates = cls._broadcast_result_txids(result)
        if not candidates:
            raise PayoutExecutionError(
                "TRON broadcast result did not include a transaction id",
                code="BROADCAST_TXID_MISSING",
                status_code=503,
            )
        if expected not in candidates:
            raise PayoutExecutionError(
                "TRON broadcast transaction id does not match signed transaction",
                code="BROADCAST_TXID_MISMATCH",
                status_code=503,
            )
        metadata = json.loads(row["chain_check_metadata"] or "{}")
        metadata["broadcast_txid_verified"] = True
        metadata["broadcast_txid_candidates"] = candidates
        return cls._transition(
            row,
            row["state"],
            chain_check_metadata=cls._json(metadata),
        )

    @classmethod
    def _mark_broadcasted(cls, row, txid, result):
        metadata = json.loads(row["chain_check_metadata"] or "{}")
        metadata["broadcast_result"] = result
        return cls._transition(
            row,
            STATE_BROADCASTED,
            txid=txid,
            chain_check_metadata=cls._json(metadata),
            txids_json=cls._json([txid]),
            reconciliation_required=0,
        )

    @classmethod
    def _refresh_chain_status(cls, row):
        if row["state"] not in (STATE_BROADCASTED, STATE_CONFIRMING):
            return row
        from .payout_status import refresh_tron_usdt_confirmation

        try:
            result = refresh_tron_usdt_confirmation(row)
        except Exception as exc:
            result = {
                "state": STATE_CONFIRMING,
                "metadata": {
                    "confirmation_check": "TRON_USDT_TRC20_TRANSFER",
                    "txid": row["txid"],
                    "transfer_match": False,
                    "error": str(exc),
                },
            }
        if result["state"] == STATE_CONFIRMING and row["state"] == STATE_CONFIRMING:
            return row

        metadata = json.loads(row["chain_check_metadata"] or "{}")
        metadata["confirmation"] = result["metadata"]
        metadata.update(result["metadata"])
        fields = {
            "chain_check_metadata": cls._json(metadata),
        }
        if result["state"] == STATE_CONFIRMED:
            fields.update(
                {
                    "txids_json": cls._json([row["txid"]]),
                    "reconciliation_required": 0,
                }
            )
        elif result["state"] == STATE_FAILED_CHAIN_TERMINAL:
            fields.update(
                {
                    "failure_class": result["failure_class"],
                    "error_code": result["error_code"],
                    "error_message": result["error_message"],
                    "reconciliation_required": 0,
                }
            )
        return cls._transition(row, result["state"], **fields)

    @classmethod
    def _mark_failed_or_reconciliation(cls, execution_id, exc):
        row = cls._get_row(execution_id)
        if row is None:
            raise exc
        if row["state"] in NO_DOWNGRADE_STATES:
            return cls._row_to_status(row)
        if (
            isinstance(exc, PayoutExecutionError)
            and exc.code == "PAYOUT_EXECUTION_CAS_CONFLICT"
        ):
            return cls._row_to_status(row)
        if (
            isinstance(exc, PayoutExecutionError)
            and exc.code == "PAYOUT_RESOURCE_LOCK_UNAVAILABLE"
            and not cls._has_unsafe_side_effect(row)
        ):
            row = cls._transition(
                row,
                STATE_RECEIVED,
                lease_owner=None,
                lease_expires_at=None,
                attempt_id=None,
                failure_class="TRANSIENT",
                error_code=exc.code,
                error_message=str(exc),
                reconciliation_required=0,
            )
            return cls._row_to_status(row)
        if cls._has_unsafe_side_effect(row):
            row = cls._transition(
                row,
                STATE_RECONCILIATION_REQUIRED,
                failure_class="AMBIGUOUS",
                error_code=getattr(exc, "code", None) or "UNSAFE_EXECUTION_INTERRUPTED",
                error_message=str(exc),
                reconciliation_required=1,
            )
        else:
            row = cls._transition(
                row,
                STATE_FAILED_PRE_BROADCAST,
                failure_class="PREFLIGHT",
                error_code=getattr(exc, "code", None)
                or "EXECUTION_PRE_BROADCAST_FAILED",
                error_message=str(exc),
                reconciliation_required=0,
            )
        return cls._row_to_status(row)

    @classmethod
    def _ensure_sufficient_wallet_balance(cls, wallet, amount):
        try:
            balance = wallet.balance
        except AttributeError:
            return
        except Exception as exc:
            raise PayoutExecutionError(
                f"Unable to read fee-deposit USDT balance: {exc}",
                code="PAYOUT_BALANCE_UNAVAILABLE",
                status_code=503,
            ) from exc
        if balance < amount:
            raise PayoutExecutionError(
                "Fee-deposit wallet does not have enough USDT for payout",
                code="INSUFFICIENT_USDT",
                status_code=409,
            )

    @staticmethod
    def _source_wallet_address_from_wallet(wallet):
        main_account = getattr(wallet, "main_account", None)
        if isinstance(main_account, dict):
            return main_account.get("public")
        if isinstance(main_account, str):
            return main_account
        return None

    @classmethod
    def execute(
        cls,
        execution_id,
        *,
        wallet,
        resource_ensurer,
        lock_factory=None,
        lease_owner=None,
    ):
        row = cls._get_row(execution_id)
        if row is None:
            raise PayoutExecutionError(
                "Payout execution was not created",
                code="NO_EXECUTION_CREATED",
                status_code=404,
            )
        try:
            if row["state"] in UNSAFE_RECOVERY_STATES:
                status = cls.recover_stale_execution(execution_id)
                row = cls._get_row(execution_id)
                if row["state"] in UNSAFE_RECOVERY_STATES:
                    return status
            if row["state"] == STATE_RECEIVED:
                row = cls._transition(row, STATE_VALIDATED)
            if row["state"] == STATE_VALIDATED:
                attempt_id = str(uuid.uuid4())
                lease_expires_at = _format_datetime(
                    _utcnow() + timedelta(seconds=config.PAYOUT_EXECUTION_LEASE_TTL_SEC)
                )
                row = cls._transition(
                    row,
                    STATE_SIGNING,
                    lease_owner=lease_owner or "payout-execution-worker",
                    lease_expires_at=lease_expires_at,
                    attempt_id=attempt_id,
                )
        except PayoutExecutionError as exc:
            if exc.code != "PAYOUT_EXECUTION_CAS_CONFLICT":
                raise
            row = cls._get_row(execution_id)
            return cls._row_to_status(row)
        attempt_id = row["attempt_id"]
        if row["state"] not in (STATE_SIGNING, STATE_SIGNED, STATE_BROADCASTING):
            return cls._row_to_status(row)

        payload = json.loads(row["canonical_payload_json"])
        amount = Decimal(payload["amount"])
        destination = payload["destination"]
        lock = lock_factory() if lock_factory else nullcontext()
        try:
            with lock:
                row = cls._get_row(execution_id)
                if row is None:
                    raise PayoutExecutionError(
                        "Payout execution was not created",
                        code="NO_EXECUTION_CREATED",
                        status_code=404,
                    )
                if (
                    row["state"] not in (STATE_SIGNING, STATE_SIGNED, STATE_BROADCASTING)
                    or row["attempt_id"] != attempt_id
                ):
                    return cls._row_to_status(row)
                cls._ensure_sufficient_wallet_balance(wallet, amount)
                if not row["resource_reservation_id"]:
                    row = cls._mark_resource_reserved(
                        row,
                        f"resource:{row['execution_id']}:{row['attempt_id']}",
                    )
                if row["signed_raw_tx_hash"]:
                    raise RuntimeError(
                        "Signed transaction evidence exists but signed transaction "
                        "artifact is not available in worker memory"
                    )
                resource_ensurer(destination, amount, tron_client=wallet.client)
                signed_tx = wallet.build_signed_transfer(
                    destination,
                    amount,
                    expiration_ms=(
                        config.TRON_USDT_PAYOUT_TX_EXPIRATION_CAP_SEC * 1000
                    ),
                )
                evidence = wallet.signed_transfer_evidence(signed_tx)
                source_wallet_address = cls._source_wallet_address_from_wallet(wallet)
                if source_wallet_address:
                    evidence.setdefault("chain_check_metadata", {})[
                        "source_wallet_address"
                    ] = source_wallet_address
                row = cls._mark_signed(row, evidence)
                row = cls._mark_broadcasting(row)
                tx_result = wallet.broadcast_signed_transfer(signed_tx)
                row = cls._verify_broadcast_result(row, signed_tx.txid, tx_result)
                row = cls._mark_broadcasted(row, signed_tx.txid, tx_result)
                return cls._row_to_status(row)
        except Exception as exc:
            return cls._mark_failed_or_reconciliation(execution_id, exc)

    @classmethod
    def _existing_or_conflict_after_integrity_error(cls, db, payload, canonical):
        existing = db.execute(
            """
            SELECT * FROM payout_executions
            WHERE execution_id = ? OR (consumer = ? AND external_id = ?)
            """,
            (
                canonical["execution_id"],
                canonical["consumer"],
                canonical["external_id"],
            ),
        ).fetchone()
        if (
            existing
            and existing["request_hash"] == payload["request_hash"]
            and existing["sidecar_payload_hash"] == payload["sidecar_payload_hash"]
        ):
            return cls._row_to_status(existing, status="ACCEPTED")
        raise PayoutExecutionError(
            "Payout execution already exists with different payload",
            code="PAYOUT_EXECUTION_CONFLICT",
            status_code=409,
        )

    @staticmethod
    def enqueue_execution(execution_id, queue, task=None):
        if task is None:
            from .tasks import execute_payout_execution

            task = execute_payout_execution

        return task.apply_async(
            args=[str(execution_id)],
            headers={"payout_enqueued_at": _format_datetime(_utcnow())},
            queue=queue,
        )

    @classmethod
    def _safe_recover_for_enqueue(cls, row):
        if (
            row["state"] in UNSAFE_RECOVERY_STATES
            and cls._lease_expired(row)
        ):
            try:
                cls.recover_stale_execution(row["execution_id"])
            except PayoutExecutionError as exc:
                if exc.code != "PAYOUT_EXECUTION_CAS_CONFLICT":
                    raise
            return cls._get_row(row["execution_id"])
        return row

    @classmethod
    def _enqueue_if_enabled(cls, row):
        cls._ensure_auto_enqueue_enabled()
        row = cls._safe_recover_for_enqueue(row)
        if row["state"] in (STATE_RECEIVED, STATE_VALIDATED):
            cls.enqueue_execution(row["execution_id"], row["payout_queue"])
        return row

    @staticmethod
    def _ensure_auto_enqueue_enabled():
        if config.PAYOUT_EXECUTION_AUTO_ENQUEUE_ENABLED:
            return
        raise PayoutExecutionError(
            "Payout execution auto-enqueue is disabled",
            code="PAYOUT_EXECUTION_AUTO_ENQUEUE_DISABLED",
            status_code=503,
        )

    @classmethod
    def preflight(
        cls,
        payload,
        *,
        authenticated_consumer,
        execution_id=None,
        endpoint_symbol="USDT",
    ):
        canonical = cls.validate_payload(
            payload,
            authenticated_consumer=authenticated_consumer,
            execution_id=execution_id,
            endpoint_symbol=endpoint_symbol,
        )
        if config.PAYOUT_EXECUTION_PREFLIGHT_CHECKS_ENABLED:
            from .payout_status import PayoutStatusError, run_tron_usdt_preflight_checks

            try:
                runtime = run_tron_usdt_preflight_checks(canonical)
            except PayoutStatusError as exc:
                raise PayoutExecutionError(
                    str(exc),
                    code=exc.code,
                    status_code=exc.status_code,
                ) from exc
        else:
            runtime = {}
        return {
            "status": "OK",
            "execution_id": canonical["execution_id"],
            "state": "PREFLIGHT_OK",
            "sidecar_payload_hash": payload["sidecar_payload_hash"],
            **runtime,
        }

    @classmethod
    def submit(
        cls,
        payload,
        *,
        authenticated_consumer,
        execution_id=None,
        endpoint_symbol="USDT",
    ):
        canonical = cls.validate_payload(
            payload,
            authenticated_consumer=authenticated_consumer,
            execution_id=execution_id,
            endpoint_symbol=endpoint_symbol,
        )
        db = get_db()
        existing = db.execute(
            "SELECT * FROM payout_executions WHERE execution_id = ?",
            (canonical["execution_id"],),
        ).fetchone()
        if existing:
            if (
                existing["request_hash"] != payload["request_hash"]
                or existing["sidecar_payload_hash"] != payload["sidecar_payload_hash"]
            ):
                raise PayoutExecutionError(
                    "Payout execution already exists with different payload",
                    code="PAYOUT_EXECUTION_CONFLICT",
                    status_code=409,
                )
            existing = cls._enqueue_if_enabled(existing)
            return cls._row_to_status(existing, status="ACCEPTED")

        consumer_existing = db.execute(
            """
            SELECT * FROM payout_executions
            WHERE consumer = ? AND external_id = ?
            """,
            (canonical["consumer"], canonical["external_id"]),
        ).fetchone()
        if consumer_existing:
            if (
                consumer_existing["request_hash"] != payload["request_hash"]
                or consumer_existing["sidecar_payload_hash"] != payload["sidecar_payload_hash"]
            ):
                raise PayoutExecutionError(
                    "Payout external_id already exists with different payload",
                    code="PAYOUT_EXECUTION_CONFLICT",
                    status_code=409,
                )
            consumer_existing = cls._enqueue_if_enabled(consumer_existing)
            return cls._row_to_status(consumer_existing, status="ACCEPTED")

        cls._ensure_auto_enqueue_enabled()
        state_transition_id = str(uuid.uuid4())
        try:
            db.execute(
                f"""
                INSERT INTO payout_executions (
                    execution_id,
                    consumer,
                    external_id,
                    request_hash,
                    sidecar_payload_hash,
                    state,
                    state_version,
                    state_transition_id,
                    state_updated_at,
                    canonical_payload_json,
                    source_wallet,
                    token_contract,
                    chain_id_or_network_id,
                    payout_queue,
                    reconciliation_required,
                    txids_json,
                    message_hashes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, {_now_expr()}, ?, ?, ?, ?, ?, 0, '[]', '[]')
                """,
                (
                    canonical["execution_id"],
                    canonical["consumer"],
                    canonical["external_id"],
                    payload["request_hash"],
                    payload["sidecar_payload_hash"],
                    STATE_RECEIVED,
                    1,
                    state_transition_id,
                    compact_json(canonical),
                    payload.get("source_wallet_ref") or "fee_deposit",
                    config.get_contract_address(TronSymbol.USDT),
                    str(config.TRON_NETWORK.value if hasattr(config.TRON_NETWORK, "value") else config.TRON_NETWORK),
                    payload.get("payout_queue") or config.TRON_USDT_PAYOUT_QUEUE,
                ),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            return cls._existing_or_conflict_after_integrity_error(
                db,
                payload,
                canonical,
            )
        row = db.execute(
            "SELECT * FROM payout_executions WHERE execution_id = ?",
            (canonical["execution_id"],),
        ).fetchone()
        row = cls._enqueue_if_enabled(row)
        return cls._row_to_status(row, status="ACCEPTED")

    @classmethod
    def status(cls, execution_id, *, authenticated_consumer, endpoint_symbol="USDT"):
        cls.validate_endpoint_symbol(endpoint_symbol)
        row = get_db().execute(
            """
            SELECT * FROM payout_executions
            WHERE execution_id = ? AND consumer = ?
            """,
            (str(execution_id), authenticated_consumer),
        ).fetchone()
        if row is None:
            raise PayoutExecutionError(
                "Payout execution was not created",
                code="NO_EXECUTION_CREATED",
                status_code=404,
            )
        if row["state"] in UNSAFE_RECOVERY_STATES:
            try:
                cls.recover_stale_execution(execution_id)
            except PayoutExecutionError as exc:
                if exc.code != "PAYOUT_EXECUTION_CAS_CONFLICT":
                    raise
            row = cls._get_row(execution_id)
        try:
            row = cls._refresh_chain_status(row)
        except PayoutExecutionError as exc:
            if exc.code != "PAYOUT_EXECUTION_CAS_CONFLICT":
                raise
            row = cls._get_row(execution_id)
        return cls._row_to_status(row)

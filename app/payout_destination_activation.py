from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import time

import redis

from .config import config
from .logging import logger
from .payout_observability import record_destination_activation
from .resource_providers.profeex import ProfeeXOrderError, ProfeeXProvider


ACTIVATION_UNAVAILABLE = "PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE"
QUOTE_UNAVAILABLE = "PAYOUT_DESTINATION_ACTIVATION_QUOTE_UNAVAILABLE"
PENDING_RECORD_STATUSES = {"QUEUED", "PENDING", "PROCESSING"}
SUCCESS_RECORD_STATUSES = {"ACTIVE", "COMPLETED"}
FAILED_RECORD_STATUSES = {"FAILED", "CANCELLED"}


@dataclass
class DestinationActivationResult:
    activated: bool
    task_id: str | None = None
    status: str | None = None


class DestinationActivationError(RuntimeError):
    def __init__(self, message, *, code, temporary):
        super().__init__(message)
        self.code = code
        self.temporary = temporary


def activation_lock_key(destination: str) -> str:
    return f"tron_usdt_destination_activation_lock:{destination}"


def activation_record_key(destination: str) -> str:
    return f"tron_usdt_destination_activation:{destination}"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redis_client():
    return redis.Redis.from_url(f"redis://{config.REDIS_HOST}")


def _quote_status(destination: str, quote_fn) -> str:
    try:
        quote = quote_fn(destination)
    except Exception as exc:
        raise DestinationActivationError(
            "Unable to quote TRON destination activation status",
            code=QUOTE_UNAVAILABLE,
            temporary=True,
        ) from exc
    if not isinstance(quote, dict):
        raise DestinationActivationError(
            "TRON destination activation quote is unavailable",
            code=QUOTE_UNAVAILABLE,
            temporary=True,
        )
    if quote.get("is_new_address") is False:
        return "active"
    if quote.get("is_new_address") is True:
        return "new"
    raise DestinationActivationError(
        "TRON destination activation quote has no address state",
        code=QUOTE_UNAVAILABLE,
        temporary=True,
    )


def _load_record(redis_client, destination: str) -> dict | None:
    try:
        raw = redis_client.get(activation_record_key(destination))
    except redis.exceptions.RedisError as exc:
        raise DestinationActivationError(
            "Unable to read TRON destination activation record",
            code=ACTIVATION_UNAVAILABLE,
            temporary=True,
        ) from exc
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _store_record(redis_client, destination: str, record: dict) -> None:
    record = dict(record)
    record["destination"] = destination
    record["updated_at"] = _utcnow()
    record.setdefault("created_at", record["updated_at"])
    try:
        redis_client.setex(
            activation_record_key(destination),
            config.TRON_USDT_DESTINATION_ACTIVATION_RECORD_TTL_SEC,
            json.dumps(record, sort_keys=True, separators=(",", ":")),
        )
    except redis.exceptions.RedisError as exc:
        raise DestinationActivationError(
            "Unable to store TRON destination activation record",
            code=ACTIVATION_UNAVAILABLE,
            temporary=True,
        ) from exc


def _raise_activation_error(exc: ProfeeXOrderError) -> None:
    code = exc.error_code or ACTIVATION_UNAVAILABLE
    mapped = {
        "DUPLICATE_REQUEST": "PAYOUT_DESTINATION_ACTIVATION_DUPLICATE",
        "REQUEST_TIMEOUT": "PAYOUT_DESTINATION_ACTIVATION_TIMEOUT",
        "SERVICE_UNAVAILABLE": ACTIVATION_UNAVAILABLE,
        "RATE_LIMIT_EXCEEDED": ACTIVATION_UNAVAILABLE,
        "INSUFFICIENT_BALANCE": ACTIVATION_UNAVAILABLE,
    }.get(code, code)
    raise DestinationActivationError(
        str(exc), code=mapped, temporary=exc.temporary
    ) from exc


def _task_id_from_order(order: dict) -> str:
    if not isinstance(order, dict) or not isinstance(order.get("task_id"), str):
        raise DestinationActivationError(
            "ProfeeX activation response has no valid task_id",
            code="PAYOUT_DESTINATION_ACTIVATION_INVALID_RESPONSE",
            temporary=False,
        )
    return order["task_id"]


def _failed_record_error(record: dict) -> DestinationActivationError:
    code = record.get("error_code") or "PAYOUT_DESTINATION_ACTIVATION_FAILED"
    return DestinationActivationError(
        "TRON destination activation previously failed",
        code=str(code),
        temporary=False,
    )


def _record_task_id(record: dict) -> str | None:
    task_id = record.get("task_id")
    return task_id if isinstance(task_id, str) else None


def ensure_destination_activated(
    destination: str,
    *,
    quote_fn,
    provider: ProfeeXProvider | None = None,
    redis_client=None,
) -> DestinationActivationResult:
    started_at = time.monotonic()
    metric_result = "terminal_error"
    lock = None
    lock_acquired = False
    try:
        if _quote_status(destination, quote_fn) == "active":
            metric_result = "success"
            return DestinationActivationResult(activated=False, status="ALREADY_ACTIVE")

        provider = provider or ProfeeXProvider()
        redis_client = redis_client or _redis_client()
        lock = redis_client.lock(
            activation_lock_key(destination),
            timeout=config.TRON_USDT_DESTINATION_ACTIVATION_LOCK_TTL_SEC,
            blocking_timeout=config.TRON_USDT_DESTINATION_ACTIVATION_LOCK_WAIT_SEC,
            thread_local=False,
        )
        try:
            acquired = lock.acquire(blocking=True)
        except redis.exceptions.RedisError as exc:
            raise DestinationActivationError(
                "Unable to acquire TRON destination activation lock",
                code="PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE",
                temporary=True,
            ) from exc
        if not acquired:
            raise DestinationActivationError(
                "Timed out waiting for TRON destination activation lock",
                code="PAYOUT_DESTINATION_ACTIVATION_PENDING",
                temporary=True,
            )
        lock_acquired = True

        if _quote_status(destination, quote_fn) == "active":
            metric_result = "success"
            return DestinationActivationResult(activated=False, status="ALREADY_ACTIVE")

        record = _load_record(redis_client, destination)
        order = None
        task_id = None
        if record:
            record_status = str(record.get("status") or "").upper()
            if record_status in FAILED_RECORD_STATUSES:
                raise _failed_record_error(record)
            if record_status in SUCCESS_RECORD_STATUSES:
                if _quote_status(destination, quote_fn) == "active":
                    metric_result = "success"
                    return DestinationActivationResult(
                        activated=True,
                        task_id=_record_task_id(record),
                        status=record_status,
                    )
                raise DestinationActivationError(
                    "TRON destination activation record is stale",
                    code="PAYOUT_DESTINATION_ACTIVATION_PENDING",
                    temporary=True,
                )
        else:
            record_status = None
        if (
            record
            and _record_task_id(record)
            and record_status in PENDING_RECORD_STATUSES
        ):
            task_id = _record_task_id(record)
            order = {
                "task_id": task_id,
                "status": record_status,
                "target": destination,
            }
        if order is None:
            try:
                order = provider.activate_address(destination)
            except ProfeeXOrderError as exc:
                if exc.error_code == "DUPLICATE_REQUEST":
                    if _quote_status(destination, quote_fn) == "active":
                        metric_result = "success"
                        return DestinationActivationResult(
                            activated=False, status="ALREADY_ACTIVE"
                        )
                _raise_activation_error(exc)
            task_id = _task_id_from_order(order)
            _store_record(redis_client, destination, order)

        settings = config.PROFEEX
        if settings is None:
            raise DestinationActivationError(
                "PROFEEX config is missing. Cannot wait for destination activation.",
                code="CONFIGURATION_ERROR",
                temporary=False,
            )
        try:
            active_order = provider.wait_for_activation(settings, task_id, order)
        except ProfeeXOrderError as exc:
            try:
                _store_record(
                    redis_client,
                    destination,
                    {
                        "task_id": task_id,
                        "status": "PROCESSING" if exc.temporary else "FAILED",
                        "error_code": exc.error_code,
                        "error_message": str(exc),
                    },
                )
            except DestinationActivationError as store_exc:
                logger.warning(
                    "TRON destination activation failure record store failed: "
                    "destination=%s task_id=%s code=%s",
                    destination,
                    task_id,
                    store_exc.code,
                )
            _raise_activation_error(exc)

        _store_record(redis_client, destination, active_order)
        if _quote_status(destination, quote_fn) != "active":
            raise DestinationActivationError(
                "TRON destination is still not active after ProfeeX activation",
                code="PAYOUT_DESTINATION_ACTIVATION_PENDING",
                temporary=True,
            )
        logger.info(
            "TRON destination activation complete: destination=%s task_id=%s status=%s",
            destination,
            task_id,
            active_order.get("status"),
        )
        metric_result = "success"
        return DestinationActivationResult(
            activated=True,
            task_id=task_id,
            status=active_order.get("status"),
        )
    except DestinationActivationError as exc:
        metric_result = "retryable_error" if exc.temporary else "terminal_error"
        raise
    finally:
        if lock_acquired:
            try:
                lock.release()
            except redis.exceptions.RedisError:
                logger.warning(
                    "TRON destination activation lock release failed: %s", destination
                )
        record_destination_activation(metric_result, time.monotonic() - started_at)

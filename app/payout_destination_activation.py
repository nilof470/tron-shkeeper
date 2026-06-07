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


def _is_active_quote(quote: dict | None) -> bool:
    return bool(quote) and quote.get("is_new_address") is False


def _load_record(redis_client, destination: str) -> dict | None:
    raw = redis_client.get(activation_record_key(destination))
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
    redis_client.setex(
        activation_record_key(destination),
        config.TRON_USDT_DESTINATION_ACTIVATION_RECORD_TTL_SEC,
        json.dumps(record, sort_keys=True, separators=(",", ":")),
    )


def _raise_activation_error(exc: ProfeeXOrderError) -> None:
    code = exc.error_code or "PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE"
    mapped = {
        "DUPLICATE_REQUEST": "PAYOUT_DESTINATION_ACTIVATION_DUPLICATE",
        "REQUEST_TIMEOUT": "PAYOUT_DESTINATION_ACTIVATION_TIMEOUT",
        "SERVICE_UNAVAILABLE": "PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE",
        "RATE_LIMIT_EXCEEDED": "PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE",
        "INSUFFICIENT_BALANCE": "PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE",
    }.get(code, code)
    raise DestinationActivationError(
        str(exc), code=mapped, temporary=exc.temporary
    ) from exc


def ensure_destination_activated(
    destination: str,
    *,
    quote_fn,
    provider: ProfeeXProvider | None = None,
    redis_client=None,
) -> DestinationActivationResult:
    started_at = time.monotonic()
    metric_result = "success"
    lock = None
    lock_acquired = False
    try:
        quote = quote_fn(destination)
        if _is_active_quote(quote):
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

        quote = quote_fn(destination)
        if _is_active_quote(quote):
            return DestinationActivationResult(activated=False, status="ALREADY_ACTIVE")

        record = _load_record(redis_client, destination)
        order = None
        task_id = None
        if record and record.get("task_id") and record.get("status") in (
            "QUEUED",
            "PENDING",
            "PROCESSING",
        ):
            task_id = record["task_id"]
            order = {
                "task_id": task_id,
                "status": record["status"],
                "target": destination,
            }
        if order is None:
            try:
                order = provider.activate_address(destination)
            except ProfeeXOrderError as exc:
                if exc.error_code == "DUPLICATE_REQUEST":
                    quote = quote_fn(destination)
                    if _is_active_quote(quote):
                        return DestinationActivationResult(
                            activated=False, status="ALREADY_ACTIVE"
                        )
                _raise_activation_error(exc)
            task_id = order["task_id"]
            _store_record(redis_client, destination, order)

        try:
            active_order = provider.wait_for_activation(config.PROFEEX, task_id, order)
        except ProfeeXOrderError as exc:
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
            _raise_activation_error(exc)

        _store_record(redis_client, destination, active_order)
        quote = quote_fn(destination)
        if not _is_active_quote(quote):
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

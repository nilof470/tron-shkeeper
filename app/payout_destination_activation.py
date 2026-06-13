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
from .resource_providers.refee import RefeeProvider, RefeeProviderError


ACTIVATION_UNAVAILABLE = "PAYOUT_DESTINATION_ACTIVATION_UNAVAILABLE"
QUOTE_UNAVAILABLE = "PAYOUT_DESTINATION_ACTIVATION_QUOTE_UNAVAILABLE"
PENDING_RECORD_STATUSES = {"QUEUED", "PENDING", "PROCESSING"}
SUCCESS_RECORD_STATUSES = {"ACTIVE", "COMPLETED"}
FAILED_RECORD_STATUSES = {"FAILED", "CANCELLED", "UNKNOWN"}


@dataclass
class DestinationActivationResult:
    activated: bool
    task_id: str | None = None
    status: str | None = None
    provider: str | None = None
    txn_hash: str | None = None


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
    raise _destination_error_from_profeex(exc) from exc


def _destination_error_from_profeex(exc: ProfeeXOrderError) -> DestinationActivationError:
    code = exc.error_code or ACTIVATION_UNAVAILABLE
    mapped = {
        "DUPLICATE_REQUEST": "PAYOUT_DESTINATION_ACTIVATION_DUPLICATE",
        "REQUEST_TIMEOUT": "PAYOUT_DESTINATION_ACTIVATION_TIMEOUT",
        "SERVICE_UNAVAILABLE": ACTIVATION_UNAVAILABLE,
        "RATE_LIMIT_EXCEEDED": ACTIVATION_UNAVAILABLE,
        "INSUFFICIENT_BALANCE": ACTIVATION_UNAVAILABLE,
    }.get(code, code)
    return DestinationActivationError(str(exc), code=mapped, temporary=exc.temporary)


def _profeex_fallback_eligible(exc: ProfeeXOrderError) -> bool:
    if exc.error_code == "DUPLICATE_REQUEST":
        return False
    if exc.provider_failure is not None:
        return exc.provider_failure.fallback_eligible is True
    return exc.temporary


def _destination_error_from_refee(exc: RefeeProviderError) -> DestinationActivationError:
    mapped = {
        "SERVICE_UNAVAILABLE": ACTIVATION_UNAVAILABLE,
        "INSUFFICIENT_BALANCE": ACTIVATION_UNAVAILABLE,
        "CONFIGURATION_ERROR": "CONFIGURATION_ERROR",
        "SCHEMA_ERROR": "PAYOUT_DESTINATION_ACTIVATION_INVALID_RESPONSE",
        "UNKNOWN_ERROR": ACTIVATION_UNAVAILABLE,
    }.get(exc.error_code or "", exc.error_code or ACTIVATION_UNAVAILABLE)
    return DestinationActivationError(str(exc), code=mapped, temporary=exc.temporary)


def _activation_provider_chain(provider, activation_providers):
    if activation_providers is not None:
        return list(activation_providers)
    if provider is not None:
        return [("profeex", provider)]
    providers = [("profeex", ProfeeXProvider())]
    if getattr(config, "TRON_USDT_RESOURCE_FALLBACK_PROVIDER", None) == "refee":
        providers.append(("refee", RefeeProvider()))
    return providers


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
    activation_providers=None,
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

        providers = _activation_provider_chain(provider, activation_providers)
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
                        provider=record.get("provider"),
                        txn_hash=record.get("txn_hash"),
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
        last_error = None
        for provider_name, activation_provider in providers:
            if provider_name == "refee":
                try:
                    refee_result = activation_provider.activate_address(destination)
                except RefeeProviderError as exc:
                    last_error = _destination_error_from_refee(exc)
                    if exc.temporary:
                        continue
                    raise last_error from exc

                if (
                    isinstance(refee_result, dict)
                    and refee_result.get("status") == "already_active"
                ):
                    metric_result = "success"
                    return DestinationActivationResult(
                        activated=False,
                        status="ALREADY_ACTIVE",
                        provider="refee",
                    )
                txn_hash = (
                    refee_result.get("txn_hash")
                    if isinstance(refee_result, dict)
                    else None
                )
                if not txn_hash:
                    raise DestinationActivationError(
                        "re:Fee activation response has no transaction hash",
                        code="PAYOUT_DESTINATION_ACTIVATION_INVALID_RESPONSE",
                        temporary=False,
                    )
                record = {
                    "provider": "refee",
                    "txn_hash": txn_hash,
                    "status": "COMPLETED",
                    "target": destination,
                }
                _store_record(redis_client, destination, record)
                logger.info(
                    "TRON destination activation complete via re:Fee: "
                    "destination=%s txn_hash=%s",
                    destination,
                    txn_hash,
                )
                metric_result = "success"
                return DestinationActivationResult(
                    activated=True,
                    status="COMPLETED",
                    provider="refee",
                    txn_hash=txn_hash,
                )

            if order is None:
                try:
                    order = activation_provider.activate_address(destination)
                except ProfeeXOrderError as exc:
                    if exc.error_code == "DUPLICATE_REQUEST":
                        if _quote_status(destination, quote_fn) == "active":
                            metric_result = "success"
                            return DestinationActivationResult(
                                activated=False, status="ALREADY_ACTIVE"
                            )
                    last_error = _destination_error_from_profeex(exc)
                    if _profeex_fallback_eligible(exc):
                        continue
                    raise last_error from exc
                except DestinationActivationError as exc:
                    last_error = exc
                    if exc.temporary:
                        continue
                    raise
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
                active_order = activation_provider.wait_for_activation(
                    settings, task_id, order
                )
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
                last_error = _destination_error_from_profeex(exc)
                raise last_error from exc
            except DestinationActivationError:
                raise

            _store_record(redis_client, destination, active_order)
            if _quote_status(destination, quote_fn) != "active":
                raise DestinationActivationError(
                    "TRON destination is still not active after ProfeeX activation",
                    code="PAYOUT_DESTINATION_ACTIVATION_PENDING",
                    temporary=True,
                )
            logger.info(
                "TRON destination activation complete: "
                "destination=%s task_id=%s status=%s",
                destination,
                task_id,
                active_order.get("status"),
            )
            metric_result = "success"
            return DestinationActivationResult(
                activated=True,
                task_id=task_id,
                status=active_order.get("status"),
                provider=active_order.get("provider"),
            )
        if last_error is not None:
            raise last_error
        raise DestinationActivationError(
            "No TRON destination activation providers are configured",
            code=ACTIVATION_UNAVAILABLE,
            temporary=False,
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

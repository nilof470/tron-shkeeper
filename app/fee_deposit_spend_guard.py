from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

import redis

from .config import config
from .db import query_db2
from .logging import logger


_fee_deposit_lock_depth = ContextVar("fee_deposit_spend_lock_depth", default=0)


def fee_deposit_address():
    row = query_db2('select * from keys where type = "fee_deposit" ', one=True)
    return row["public"] if row else None


def is_fee_deposit_address(address):
    return bool(address) and address == fee_deposit_address()


@contextmanager
def fee_deposit_spend_lock(reason=None):
    if not config.TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED:
        yield
        return

    depth = _fee_deposit_lock_depth.get()
    token = _fee_deposit_lock_depth.set(depth + 1)
    if depth > 0:
        try:
            yield
        finally:
            _fee_deposit_lock_depth.reset(token)
        return

    from .payout_execution import PayoutExecutionError

    client = redis.Redis.from_url(f"redis://{config.REDIS_HOST}")
    lock = client.lock(
        "tron_usdt_fee_payout_resources",
        timeout=config.TRON_USDT_PAYOUT_RESOURCE_LOCK_TTL_SEC,
        blocking_timeout=config.TRON_USDT_PAYOUT_RESOURCE_LOCK_WAIT_SEC,
        thread_local=False,
    )
    try:
        acquired = lock.acquire(blocking=True)
    except redis.exceptions.RedisError as exc:
        _fee_deposit_lock_depth.reset(token)
        raise PayoutExecutionError(
            "Unable to acquire TRON fee-deposit spend lock",
            code="PAYOUT_RESOURCE_LOCK_UNAVAILABLE",
            status_code=503,
        ) from exc
    if not acquired:
        _fee_deposit_lock_depth.reset(token)
        raise PayoutExecutionError(
            "Timed out waiting for TRON fee-deposit spend lock",
            code="PAYOUT_RESOURCE_LOCK_UNAVAILABLE",
            status_code=503,
        )
    try:
        yield
    finally:
        try:
            lock.release()
        except redis.exceptions.RedisError:
            logger.warning(
                "TRON fee-deposit spend lock release failed: reason=%s",
                reason,
            )
        _fee_deposit_lock_depth.reset(token)


@contextmanager
def fee_deposit_spend_guard_for_address(address, reason=None):
    if is_fee_deposit_address(address):
        with fee_deposit_spend_lock(reason=reason):
            yield
    else:
        yield

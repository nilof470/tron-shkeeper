from . import celery
from .config import config
from .logging import logger


def queue_has_consumer(
    queue_name: str,
    *,
    celery_app=None,
    timeout: float | None = None,
) -> bool:
    app = celery_app or celery
    inspect_timeout = (
        timeout
        if timeout is not None
        else config.TRON_USDT_PAYOUT_QUEUE_READINESS_TIMEOUT_SEC
    )
    try:
        responses = app.control.inspect(timeout=inspect_timeout).active_queues()
    except Exception:
        logger.warning(
            f"Unable to inspect Celery queues for {queue_name}",
            exc_info=True,
        )
        return False

    if not responses:
        return False

    for queues in responses.values():
        if not isinstance(queues, list):
            continue
        for queue in queues:
            if isinstance(queue, dict) and queue.get("name") == queue_name:
                return True
    return False


def usdt_payout_worker_ready() -> bool:
    return queue_has_consumer(config.TRON_USDT_PAYOUT_QUEUE)

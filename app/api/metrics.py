import functools
import json
import re
import time
from datetime import datetime, timezone
import requests
import redis
import prometheus_client
from prometheus_client import generate_latest, Info, Gauge

from . import metrics_blueprint
from ..block_scanner import BlockScanner
from ..connection_manager import ConnectionManager


for collector in (
    prometheus_client.GC_COLLECTOR,
    prometheus_client.PLATFORM_COLLECTOR,
    prometheus_client.PROCESS_COLLECTOR,
):
    try:
        prometheus_client.REGISTRY.unregister(collector)
    except KeyError:
        pass


def get_ttl_hash(seconds=1*60*60*24):
    """Return the same value withing `seconds` time period"""
    return round(time.time() / seconds)

@functools.lru_cache(maxsize=2)
def get_latest_release(ttl_hash=None):
    try:
        data = requests.get(
            'https://api.github.com/repos/tronprotocol/java-tron/releases/latest',
            timeout=5,
        ).json()
        version = data["tag_name"].split('-v')[1]
        info = {key: data[key] for key in ["name", "tag_name", "published_at"]}
        info['version'] = version
        return info
    except Exception:
        return {
            "name": "java-tron",
            "tag_name": "unknown",
            "published_at": "unknown",
            "version": "unknown",
        }
tron_fullnode_last_release = Info(
    'tron_fullnode_last_release',
    'Version of the latest release from https://github.com/tronprotocol/java-tron/releases'
)

tron_fullnode_status = Gauge('tron_fullnode_status', '', ('server',))
tron_fullnode_version = Info('tron_fullnode_version', '', ('server',))
tron_fullnode_last_block = Gauge('tron_fullnode_last_block', '', ('server',))
tron_fullnode_last_block_ts = Gauge('tron_fullnode_last_block_ts', '', ('server',))
tron_wallet_last_block = Gauge('tron_wallet_last_block', '')
tron_wallet_last_block_ts = Gauge('tron_wallet_last_block_ts', '')
tron_has_alive_servers = Gauge('tron_has_alive_servers', '')
tron_payout_execution_count = Gauge(
    "tron_payout_execution_count",
    "TRON payout executions by sidecar state.",
    ("state", "reconciliation_required"),
)
tron_payout_non_terminal_oldest_age_seconds = Gauge(
    "tron_payout_non_terminal_oldest_age_seconds",
    "Age in seconds of the oldest non-terminal TRON payout execution by state.",
    ("state",),
)
tron_payout_reconciliation_required_count = Gauge(
    "tron_payout_reconciliation_required_count",
    "TRON payout executions currently requiring operator reconciliation.",
)
tron_payout_callback_outbox_backlog_count = Gauge(
    "tron_payout_callback_outbox_backlog_count",
    "Undelivered TRON payout callback outbox events by status.",
    ("status",),
)
tron_payout_callback_outbox_oldest_age_seconds = Gauge(
    "tron_payout_callback_outbox_oldest_age_seconds",
    "Age in seconds of the oldest undelivered TRON payout callback outbox event.",
    ("status",),
)
tron_payout_worker_ready = Gauge(
    "tron_payout_worker_ready",
    "Whether the dedicated TRON-USDT payout worker is consuming its queue.",
    ("queue",),
)
tron_payout_broker_queue_depth = Gauge(
    "tron_payout_broker_queue_depth",
    "Redis broker list length for the dedicated TRON-USDT payout queue. -1 means unavailable.",
    ("queue",),
)
tron_payout_broker_queue_oldest_age_seconds = Gauge(
    "tron_payout_broker_queue_oldest_age_seconds",
    "Age in seconds of the oldest queued TRON-USDT broker item. 0 means empty, -1 means unavailable.",
    ("queue",),
)
tron_payout_hot_wallet_balance = Gauge(
    "tron_payout_hot_wallet_balance",
    "TRON payout hot wallet token balance. -1 means unavailable.",
    ("asset", "source_wallet"),
)
tron_payout_fee_wallet_balance = Gauge(
    "tron_payout_fee_wallet_balance",
    "TRON payout fee wallet native balance. -1 means unavailable.",
    ("asset", "source_wallet"),
)
tron_payout_failure_count = Gauge(
    "tron_payout_failure_count",
    "TRON payout executions with failure metadata by failure class and bounded error code.",
    ("state", "failure_class", "error_code"),
)

TERMINAL_PAYOUT_STATES = {
    "CONFIRMED",
    "FAILED_PRE_BROADCAST",
    "FAILED_CHAIN_TERMINAL",
}
PAYOUT_STATES = (
    "RECEIVED",
    "VALIDATED",
    "SIGNING",
    "SIGNED",
    "BROADCASTING",
    "BROADCASTED",
    "CONFIRMING",
    "CONFIRMED",
    "FAILED_PRE_BROADCAST",
    "FAILED_CHAIN_TERMINAL",
    "RECONCILIATION_REQUIRED",
)
RECONCILIATION_LABELS = ("false", "true")
UNDELIVERED_CALLBACK_STATUSES = ("PENDING", "RETRY", "DISPATCHING", "FAILED")
METRIC_ERROR_CODE_RE = re.compile(r"^[A-Z0-9_:-]{1,80}$")


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _age_seconds(now, value):
    parsed = _parse_datetime(value)
    if parsed is None:
        return 0
    return max(0, int((now - parsed).total_seconds()))


def _payout_enqueued_at_from_message(message):
    if isinstance(message, bytes):
        message = message.decode("utf-8")
    payload = json.loads(message)
    headers = payload.get("headers") or {}
    return headers.get("payout_enqueued_at")


def _redis_queue_stats(redis_host, queue, now):
    try:
        client = redis.Redis.from_url(
            f"redis://{redis_host}",
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        depth = int(client.llen(queue))
        if depth <= 0:
            return depth, 0

        edge_messages = []
        edge_messages.extend(client.lrange(queue, 0, 0))
        edge_messages.extend(client.lrange(queue, -1, -1))
    except (redis.exceptions.RedisError, OSError, TypeError, ValueError):
        return -1, -1

    try:
        ages = []
        for message in edge_messages:
            enqueued_at = _payout_enqueued_at_from_message(message)
            if enqueued_at:
                ages.append(_age_seconds(now, enqueued_at))
    except (TypeError, ValueError, AttributeError, UnicodeError):
        return depth, -1
    if not ages:
        return depth, -1
    return depth, max(ages)


def _tron_wallet_balance(symbol):
    from ..wallet import Wallet

    return Wallet(symbol).balance


def _metric_number_or_unavailable(collector):
    try:
        return float(collector())
    except Exception:
        return -1


def _metric_error_code(error_code):
    if not error_code:
        return ""
    error_code = str(error_code).strip()
    if METRIC_ERROR_CODE_RE.match(error_code):
        return error_code
    return "OTHER"


def _update_wallet_balance_metrics():
    labels = {"source_wallet": "fee_deposit"}
    tron_payout_hot_wallet_balance.labels(asset="USDT", **labels).set(
        _metric_number_or_unavailable(lambda: _tron_wallet_balance("USDT"))
    )
    tron_payout_fee_wallet_balance.labels(asset="TRX", **labels).set(
        _metric_number_or_unavailable(lambda: _tron_wallet_balance("TRX"))
    )


def _clear_payout_metrics():
    tron_payout_failure_count.clear()
    for state in PAYOUT_STATES:
        for reconciliation_required in RECONCILIATION_LABELS:
            tron_payout_execution_count.labels(
                state=state,
                reconciliation_required=reconciliation_required,
            ).set(0)
        if state not in TERMINAL_PAYOUT_STATES:
            tron_payout_non_terminal_oldest_age_seconds.labels(state=state).set(0)
    tron_payout_reconciliation_required_count.set(0)
    for status in UNDELIVERED_CALLBACK_STATUSES:
        tron_payout_callback_outbox_backlog_count.labels(status=status).set(0)
        tron_payout_callback_outbox_oldest_age_seconds.labels(status=status).set(0)


def _update_worker_and_broker_metrics(now=None):
    from app.config import config
    from app.celery_readiness import usdt_payout_worker_ready

    now = now or _utcnow()
    queue = config.TRON_USDT_PAYOUT_QUEUE
    try:
        worker_ready = 1 if usdt_payout_worker_ready() else 0
    except Exception:
        worker_ready = 0
    tron_payout_worker_ready.labels(queue=queue).set(worker_ready)
    depth, oldest_age = _redis_queue_stats(config.REDIS_HOST, queue, now)
    tron_payout_broker_queue_depth.labels(queue=queue).set(depth)
    tron_payout_broker_queue_oldest_age_seconds.labels(queue=queue).set(oldest_age)
    _update_wallet_balance_metrics()


def update_payout_metrics(now=None):
    from app import db

    now = now or _utcnow()
    try:
        conn = db.get_db()
        execution_rows = conn.execute(
            """
            SELECT state, reconciliation_required, COUNT(*) AS count,
                   MIN(state_updated_at) AS oldest_state_updated_at
            FROM payout_executions
            GROUP BY state, reconciliation_required
            """
        ).fetchall()
        reconciliation_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM payout_executions
            WHERE reconciliation_required = 1
            """
        ).fetchone()["count"]
        placeholders = ",".join("?" for _ in UNDELIVERED_CALLBACK_STATUSES)
        callback_rows = conn.execute(
            f"""
            SELECT status, COUNT(*) AS count, MIN(created_at) AS oldest_created_at
            FROM payout_callback_outbox
            WHERE status IN ({placeholders})
            GROUP BY status
            """,
            tuple(UNDELIVERED_CALLBACK_STATUSES),
        ).fetchall()
        failure_rows = conn.execute(
            """
            SELECT state, failure_class, error_code, COUNT(*) AS count
            FROM payout_executions
            WHERE failure_class IS NOT NULL OR error_code IS NOT NULL
            GROUP BY state, failure_class, error_code
            """
        ).fetchall()

        _clear_payout_metrics()

        for row in execution_rows:
            reconciliation_label = "true" if row["reconciliation_required"] else "false"
            state = row["state"]
            tron_payout_execution_count.labels(
                state=state,
                reconciliation_required=reconciliation_label,
            ).set(row["count"])
            if state not in TERMINAL_PAYOUT_STATES:
                tron_payout_non_terminal_oldest_age_seconds.labels(state=state).set(
                    _age_seconds(now, row["oldest_state_updated_at"])
                )

        tron_payout_reconciliation_required_count.set(reconciliation_count)

        for row in callback_rows:
            tron_payout_callback_outbox_backlog_count.labels(status=row["status"]).set(
                row["count"]
            )
            tron_payout_callback_outbox_oldest_age_seconds.labels(
                status=row["status"]
            ).set(_age_seconds(now, row["oldest_created_at"]))

        for row in failure_rows:
            tron_payout_failure_count.labels(
                state=row["state"] or "",
                failure_class=row["failure_class"] or "",
                error_code=_metric_error_code(row["error_code"]),
            ).set(row["count"])
    finally:
        _update_worker_and_broker_metrics(now=now)

@metrics_blueprint.get("/metrics")
def get_metrics():
    try:
        bs = BlockScanner()
        last_seen_block_num = bs.get_last_seen_block_num()
        tron_wallet_last_block.set(last_seen_block_num)
        tron_wallet_last_block_ts.set(
            bs.download_block(last_seen_block_num)['block_header']['raw_data'][
                'timestamp'
            ] // 1000
        )
        tron_fullnode_last_release.info(get_latest_release(ttl_hash=get_ttl_hash()))

        tron_has_alive_servers.set(0)
        for server in ConnectionManager.manager().get_servers_status():
            if server['status'] == "success":
                tron_has_alive_servers.set(1)
                tron_fullnode_status.labels(server=server['name']).set(1)
                tron_fullnode_version.labels(server=server['name']).info({
                    'version': server["node_info"]["configNodeInfo"]["codeVersion"]
                })
                tron_fullnode_last_block.labels(server=server['name']).set(
                    server["node_info"]["block"]
                )
                tron_fullnode_last_block_ts.labels(server=server['name']).set(
                    server["node_info"]["block_ts"]
                )
            else:
                tron_fullnode_status.labels(server=server['name']).set(0)
    except Exception:
        tron_has_alive_servers.set(0)
    try:
        update_payout_metrics()
    except Exception:
        pass
    return generate_latest().decode()

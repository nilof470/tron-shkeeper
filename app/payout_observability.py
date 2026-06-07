import re

from prometheus_client import Counter, Histogram


METRIC_ERROR_CODE_RE = re.compile(r"^[A-Z0-9_:-]{1,80}$")
PAYOUT_API_OPERATIONS = {"preflight", "submit", "status"}
DESTINATION_ACTIVATION_RESULTS = {"success", "retryable_error", "terminal_error"}

tron_payout_request_failed = Counter(
    "tron_payout_request_failed",
    "TRON payout API requests rejected by operation and bounded error code.",
    ("operation", "code"),
)

tron_payout_destination_activation_total = Counter(
    "tron_payout_destination_activation",
    "TRON payout destination activation attempts by result.",
    ("result",),
)

tron_payout_destination_activation_duration_seconds = Histogram(
    "tron_payout_destination_activation_duration_seconds",
    "TRON payout destination activation duration in seconds.",
)


def _metric_operation(operation):
    operation = str(operation or "").strip().lower()
    return operation if operation in PAYOUT_API_OPERATIONS else "other"


def _metric_error_code(code):
    if not code:
        return ""
    code = str(code).strip()
    if METRIC_ERROR_CODE_RE.match(code):
        return code
    return "OTHER"


def payout_operation_from_request(method, path):
    method = str(method or "").upper()
    path = str(path or "").rstrip("/")
    if method == "GET":
        return "status"
    if path.endswith("/preflight"):
        return "preflight"
    if method == "POST":
        return "submit"
    return "other"


def record_payout_request_failed(operation, code):
    tron_payout_request_failed.labels(
        operation=_metric_operation(operation),
        code=_metric_error_code(code),
    ).inc()


def _metric_activation_result(result):
    result = str(result or "").strip().lower()
    return result if result in DESTINATION_ACTIVATION_RESULTS else "terminal_error"


def record_destination_activation(result, duration_seconds):
    tron_payout_destination_activation_total.labels(
        result=_metric_activation_result(result),
    ).inc()
    tron_payout_destination_activation_duration_seconds.observe(
        max(0.0, float(duration_seconds or 0.0))
    )


def clear_destination_activation_metrics():
    tron_payout_destination_activation_total.clear()
    # prometheus-client's Histogram.clear() needs label storage; this histogram
    # is unlabeled in the installed version, so reset its bucket state directly.
    tron_payout_destination_activation_duration_seconds._metric_init()


def clear_payout_request_metrics():
    tron_payout_request_failed.clear()
    clear_destination_activation_metrics()

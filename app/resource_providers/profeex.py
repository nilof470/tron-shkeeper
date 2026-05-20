import time

import requests

from ..config import config
from ..connection_manager import ConnectionManager
from ..logging import logger
from ..utils import has_free_bw


class ProfeeXBandwidthProvider:
    REQUEST_TIMEOUT_SEC = 10
    PENDING_STATUSES = {"QUEUED", "PENDING", "PROCESSING"}
    SUCCESS_STATUSES = {"ACTIVE"}
    FAILURE_STATUSES = {"FAILED", "CANCELLED", "COMPLETED", "unknown"}

    def __init__(self, tron_client=None):
        self.tron_client = tron_client

    def acquire_bandwidth(self, receiver: str, bandwidth_required: int) -> bool:
        settings = config.PROFEEX
        if settings is None:
            logger.warning("PROFEEX config is missing. Terminating transfer.")
            return False

        tron_client = self.tron_client or ConnectionManager.client()
        if has_free_bw(receiver, bandwidth_required, tron_client=tron_client):
            logger.info(
                f"ProfeeX bandwidth order not needed for {receiver}: "
                f"{bandwidth_required=} already available"
            )
            return True

        amount = max(bandwidth_required, settings.min_bandwidth_order_amount)
        if amount > settings.max_bandwidth_order_amount:
            logger.warning(
                "ProfeeX bandwidth request exceeds provider maximum: "
                f"{amount=} max={settings.max_bandwidth_order_amount}"
            )
            return False

        order = self._create_bandwidth_order(settings, receiver, amount)
        if order is None:
            return False

        task_id = order.get("task_id")
        if not task_id:
            logger.warning(f"ProfeeX bandwidth order response has no task_id: {order}")
            return False

        active_order = self._wait_until_active(settings, task_id, order)
        if active_order is None:
            return False

        if not has_free_bw(receiver, bandwidth_required, tron_client=tron_client):
            logger.warning(
                "Onetime account has not enough bandwidth after ProfeeX delegation. "
                "Terminating transfer."
            )
            return False

        logger.info(f"ProfeeX bandwidth successfully delegated: {active_order}")
        return True

    def _create_bandwidth_order(
        self, settings, receiver: str, amount: int
    ) -> dict | None:
        try:
            response = requests.post(
                self._url(settings, "/delegation/buybandwidth"),
                params={
                    "target": receiver,
                    "volume": amount,
                    "days": settings.bandwidth_duration_label,
                    "currency": settings.currency,
                },
                headers=self._headers(settings),
                timeout=self.REQUEST_TIMEOUT_SEC,
            )
        except requests.RequestException:
            logger.exception("ProfeeX create bandwidth order request failed")
            return None

        if response.status_code != 202:
            logger.warning(
                "ProfeeX create bandwidth order rejected with status "
                f"{response.status_code}: {response.text}"
            )
            return None

        try:
            data = response.json()
        except ValueError:
            logger.exception("ProfeeX create bandwidth order response is not valid JSON")
            return None
        if not isinstance(data, dict):
            logger.warning(
                f"ProfeeX create bandwidth order response is not an object: {data}"
            )
            return None

        logger.info(f"ProfeeX bandwidth order accepted: {data}")
        return data

    def _wait_until_active(
        self, settings, task_id: str, initial_order: dict
    ) -> dict | None:
        deadline = time.monotonic() + settings.timeout_sec
        order = initial_order
        last_status = None
        should_sleep_before_poll = False

        while True:
            status = order.get("status")
            if status != last_status:
                logger.info(f"ProfeeX bandwidth order {task_id} status: {status}")
                last_status = status

            if status in self.SUCCESS_STATUSES:
                return order
            if status in self.FAILURE_STATUSES:
                logger.warning(f"ProfeeX bandwidth order {task_id} failed: {order}")
                return None
            if status not in self.PENDING_STATUSES:
                logger.warning(
                    f"ProfeeX bandwidth order {task_id} returned unexpected status: "
                    f"{status}"
                )
                return None

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            if should_sleep_before_poll:
                sleep_for = min(settings.poll_interval_sec, remaining)
                if sleep_for > 0:
                    time.sleep(sleep_for)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

            request_timeout = min(self.REQUEST_TIMEOUT_SEC, remaining)
            if request_timeout <= 0:
                break

            try:
                response = requests.get(
                    self._url(settings, f"/delegation/status/{task_id}"),
                    headers=self._headers(settings),
                    timeout=request_timeout,
                )
            except requests.RequestException:
                logger.warning(
                    f"ProfeeX poll request failed for bandwidth order {task_id}"
                )
                should_sleep_before_poll = True
                continue

            if response.status_code != 200:
                logger.warning(
                    "ProfeeX poll for bandwidth order "
                    f"{task_id} returned status {response.status_code}: {response.text}"
                )
                should_sleep_before_poll = True
                continue

            try:
                order = response.json()
            except ValueError:
                logger.exception(
                    f"ProfeeX poll response is not valid JSON for bandwidth order {task_id}"
                )
                return None
            if not isinstance(order, dict):
                logger.warning(
                    f"ProfeeX poll response is not an object for bandwidth order {task_id}: "
                    f"{order}"
                )
                return None
            should_sleep_before_poll = True

        logger.warning(
            f"ProfeeX bandwidth order {task_id} did not reach ACTIVE status within "
            f"{settings.timeout_sec} seconds"
        )
        return None

    @staticmethod
    def _headers(settings) -> dict:
        return {"X-API-Key": settings.api_key.get_secret_value()}

    @staticmethod
    def _url(settings, path: str) -> str:
        return f"{settings.api_base_url.rstrip('/')}{path}"

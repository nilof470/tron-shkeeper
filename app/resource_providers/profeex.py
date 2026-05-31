import time

import requests

from .base import BandwidthProvider, EnergyProvider
from ..config import config
from ..connection_manager import ConnectionManager
from ..logging import logger
from ..utils import get_available_energy, has_free_bw


TEMPORARY_ERROR_CODES = {
    "DUPLICATE_REQUEST",
    "RATE_LIMIT_EXCEEDED",
    "SERVICE_UNAVAILABLE",
    "REQUEST_TIMEOUT",
}
OPERATIONAL_ERROR_CODES = {
    "INSUFFICIENT_BALANCE",
    "PROCESSING_FAILED",
    "CONFIGURATION_ERROR",
    "UNKNOWN_ERROR",
}
VALIDATION_ERROR_CODES = {
    "INVALID_ADDRESS",
    "INVALID_PARAMETERS",
}


class ProfeeXOrderError(RuntimeError):
    def __init__(self, resource_name, message, error_code=None, temporary=False):
        super().__init__(message)
        self.resource_name = resource_name
        self.error_code = error_code
        self.temporary = temporary


class ProfeeXProvider(EnergyProvider, BandwidthProvider):
    REQUEST_TIMEOUT_SEC = 10
    PENDING_STATUSES = {"QUEUED", "PENDING", "PROCESSING"}
    SUCCESS_STATUSES = {"ACTIVE"}
    FAILURE_STATUSES = {"FAILED", "CANCELLED", "COMPLETED", "unknown"}
    FIXED_ENERGY_ORDER_TOLERANCE = 500

    def __init__(self, tron_client=None):
        self.tron_client = tron_client

    def acquire_energy(
        self,
        receiver: str,
        energy_to_provision: int,
        account_resource: dict,
        *,
        minimum_energy_required: int | None = None,
        strict_minimum_required: bool = False,
    ) -> bool:
        settings = config.PROFEEX
        if settings is None:
            logger.warning("PROFEEX config is missing. Terminating transfer.")
            return False

        threshold = max(
            settings.fixed_energy_order_amount - self.FIXED_ENERGY_ORDER_TOLERANCE,
            0,
        )
        if strict_minimum_required and minimum_energy_required is not None:
            threshold = max(threshold, minimum_energy_required)
        tron_client = self.tron_client or ConnectionManager.client()
        onetime_energy_available = self._get_available_energy(
            tron_client, receiver, "pre-order"
        )
        if onetime_energy_available is None:
            return False
        if onetime_energy_available >= threshold:
            logger.info(
                f"ProfeeX energy order not needed for {receiver}: "
                f"{onetime_energy_available=} energy_threshold={threshold}"
            )
            return True

        amount = max(settings.fixed_energy_order_amount, energy_to_provision)
        logger.info(
            f"Requesting ProfeeX energy rental for {receiver}: "
            f"{amount} energy for {settings.energy_duration_label}"
        )

        order = self._create_order(
            settings,
            receiver,
            amount,
            resource_name="energy",
            path="/delegation/buyenergy",
            duration_label=settings.energy_duration_label,
        )
        if order is None:
            return False

        task_id = self._extract_task_id(order, "energy")
        if task_id is None:
            return False

        active_order = self._wait_until_active(settings, task_id, order, "energy")
        if active_order is None:
            return False

        onetime_energy_available = self._get_available_energy(
            tron_client, receiver, "post-delegation"
        )
        if onetime_energy_available is None:
            return False
        if onetime_energy_available < threshold:
            logger.warning(
                "Onetime account has not enough energy after ProfeeX delegation. "
                "Terminating transfer."
            )
            return False

        logger.info(f"ProfeeX energy successfully delegated: {active_order}")
        return True

    def release_energy(self, receiver: str) -> None:
        logger.info(
            f"ProfeeX energy for {receiver} returns after rent expiration. "
            "Skipping undelegate."
        )

    def estimate_usdt_transfer_fee(self, receiver_address: str) -> dict | None:
        settings = config.PROFEEX
        if settings is None:
            logger.warning("PROFEEX config is missing. Cannot estimate USDT fee.")
            return None

        try:
            response = requests.get(
                self._url(settings, "/delegation/fee"),
                params={"receiver_address": receiver_address},
                headers=self._headers(settings),
                timeout=self.REQUEST_TIMEOUT_SEC,
            )
        except requests.RequestException:
            logger.exception("ProfeeX USDT fee estimate request failed")
            return None

        if response.status_code != 200:
            logger.warning(
                f"ProfeeX USDT fee estimate rejected with status "
                f"{response.status_code}: {response.text}"
            )
            return None

        try:
            data = response.json()
        except ValueError:
            logger.exception("ProfeeX USDT fee estimate response is not valid JSON")
            return None
        if not isinstance(data, dict):
            logger.warning(
                f"ProfeeX USDT fee estimate response is not an object: {data}"
            )
            return None
        if type(data.get("energy_required")) is not int:
            logger.warning(f"ProfeeX USDT fee estimate has no energy_required: {data}")
            return None
        if data["energy_required"] < 0:
            logger.warning(
                f"ProfeeX USDT fee estimate has invalid energy_required: {data}"
            )
            return None
        if type(data.get("is_new_address")) is not bool:
            logger.warning(f"ProfeeX USDT fee estimate has no is_new_address flag: {data}")
            return None
        if "trx_burned" not in data:
            logger.warning(f"ProfeeX USDT fee estimate has no trx_burned field: {data}")
            return None
        return data

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

        amount = settings.fixed_bandwidth_order_amount
        if amount < bandwidth_required:
            logger.warning(
                "ProfeeX fixed bandwidth order amount is below required bandwidth: "
                f"{amount=} {bandwidth_required=}"
            )
            return False

        order = self._create_order(
            settings,
            receiver,
            amount,
            resource_name="bandwidth",
            path="/delegation/buybandwidth",
            duration_label=settings.bandwidth_duration_label,
        )
        if order is None:
            return False

        task_id = self._extract_task_id(order, "bandwidth")
        if task_id is None:
            return False

        active_order = self._wait_until_active(settings, task_id, order, "bandwidth")
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

    def _create_order(
        self,
        settings,
        receiver: str,
        amount: int,
        *,
        resource_name: str,
        path: str,
        duration_label: str,
    ) -> dict | None:
        try:
            response = requests.post(
                self._url(settings, path),
                params={
                    "target": receiver,
                    "volume": amount,
                    "days": duration_label,
                    "currency": settings.currency,
                },
                headers=self._headers(settings),
                timeout=self.REQUEST_TIMEOUT_SEC,
            )
        except requests.RequestException:
            logger.exception(f"ProfeeX create {resource_name} order request failed")
            return None

        if response.status_code != 202:
            logger.warning(
                f"ProfeeX create {resource_name} order rejected with status "
                f"{response.status_code}: {response.text}"
            )
            return None

        try:
            data = response.json()
        except ValueError:
            logger.exception(
                f"ProfeeX create {resource_name} order response is not valid JSON"
            )
            return None
        if not isinstance(data, dict):
            logger.warning(
                f"ProfeeX create {resource_name} order response is not an object: {data}"
            )
            return None

        logger.info(f"ProfeeX {resource_name} order accepted: {data}")
        return data

    def _extract_task_id(self, order: dict, resource_name: str) -> str | None:
        task_id = order.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            logger.warning(
                f"ProfeeX {resource_name} order response has no task_id: {order}"
            )
            return None
        if "status" not in order:
            order["status"] = "PENDING"
        return task_id

    def _order_error_from_order(
        self, resource_name: str, order: dict
    ) -> ProfeeXOrderError:
        error_code = order.get("error_code")
        details = order.get("details") or {}
        message = (
            details.get("error_message")
            or f"ProfeeX {resource_name} order failed: {order}"
        )
        temporary = error_code in TEMPORARY_ERROR_CODES
        return ProfeeXOrderError(resource_name, message, error_code, temporary)

    def _get_available_energy(self, tron_client, receiver: str, stage: str) -> int | None:
        try:
            return get_available_energy(tron_client.get_account_resource(receiver))
        except Exception:
            logger.exception(
                f"Unable to read ProfeeX receiver energy during {stage}: {receiver}"
            )
            return None

    def _wait_until_active(
        self, settings, task_id: str, initial_order: dict, resource_name: str
    ) -> dict | None:
        deadline = time.monotonic() + settings.timeout_sec
        order = initial_order
        last_status = None
        should_sleep_before_poll = False

        while True:
            status = order.get("status")
            if status != last_status:
                logger.info(f"ProfeeX {resource_name} order {task_id} status: {status}")
                last_status = status

            if status in self.SUCCESS_STATUSES:
                return order
            if status in self.FAILURE_STATUSES:
                logger.warning(
                    f"ProfeeX {resource_name} order {task_id} failed: {order}"
                )
                return None
            if status not in self.PENDING_STATUSES:
                logger.warning(
                    f"ProfeeX {resource_name} order {task_id} returned unexpected "
                    f"status: {status}"
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
                    f"ProfeeX poll request failed for {resource_name} order {task_id}"
                )
                should_sleep_before_poll = True
                continue

            if response.status_code != 200:
                logger.warning(
                    f"ProfeeX poll for {resource_name} order {task_id} returned "
                    f"status {response.status_code}: {response.text}"
                )
                should_sleep_before_poll = True
                continue

            try:
                order = response.json()
            except ValueError:
                logger.exception(
                    f"ProfeeX poll response is not valid JSON for "
                    f"{resource_name} order {task_id}"
                )
                return None
            if not isinstance(order, dict):
                logger.warning(
                    f"ProfeeX poll response is not an object for "
                    f"{resource_name} order {task_id}: {order}"
                )
                return None
            should_sleep_before_poll = True

        logger.warning(
            f"ProfeeX {resource_name} order {task_id} did not reach ACTIVE status "
            f"within {settings.timeout_sec} seconds"
        )
        return None

    @staticmethod
    def _headers(settings) -> dict:
        return {"X-API-Key": settings.api_key.get_secret_value()}

    @staticmethod
    def _url(settings, path: str) -> str:
        return f"{settings.api_base_url.rstrip('/')}{path}"


ProfeeXBandwidthProvider = ProfeeXProvider

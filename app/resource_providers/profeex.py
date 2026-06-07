from __future__ import annotations

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
    ACTIVATION_SUCCESS_STATUSES = {"ACTIVE", "COMPLETED"}
    ACTIVATION_FAILURE_STATUSES = {"FAILED", "CANCELLED", "unknown"}
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

        if not self._wait_for_energy_available(
            tron_client,
            receiver,
            threshold,
            "post-delegation",
        ):
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

    def activate_address(self, receiver: str) -> dict:
        settings = config.PROFEEX
        if settings is None:
            raise ProfeeXOrderError(
                "activation",
                "PROFEEX config is missing. Cannot activate destination.",
                "CONFIGURATION_ERROR",
                temporary=False,
            )
        try:
            response = requests.post(
                self._url(settings, "/activation/activate"),
                params={
                    "address": receiver,
                    "currency": settings.currency,
                },
                headers=self._headers(settings),
                timeout=self.REQUEST_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            raise ProfeeXOrderError(
                "activation",
                f"ProfeeX activation request failed: {exc}",
                "SERVICE_UNAVAILABLE",
                temporary=True,
            ) from exc

        if response.status_code == 202:
            data = self._json_response(response, "activation")
            task_id = self._extract_task_id(data, "activation")
            if task_id is None:
                raise ProfeeXOrderError(
                    "activation",
                    f"ProfeeX activation response has no task_id: {data}",
                    "INVALID_PARAMETERS",
                    temporary=False,
                )
            logger.info(f"ProfeeX activation accepted: {data}")
            return data

        if response.status_code == 409:
            raise ProfeeXOrderError(
                "activation",
                f"ProfeeX activation duplicate or already active: {response.text}",
                "DUPLICATE_REQUEST",
                temporary=True,
            )
        if response.status_code == 503:
            raise ProfeeXOrderError(
                "activation",
                f"ProfeeX activation unavailable: {response.text}",
                "SERVICE_UNAVAILABLE",
                temporary=True,
            )

        data = self._safe_json(response)
        code = self._error_code_from_payload(data)
        temporary = code in TEMPORARY_ERROR_CODES or code in OPERATIONAL_ERROR_CODES
        raise ProfeeXOrderError(
            "activation",
            f"ProfeeX activation rejected with status {response.status_code}: {response.text}",
            code or "UNKNOWN_ERROR",
            temporary=temporary,
        )

    def wait_for_activation(self, settings, task_id: str, initial_order: dict) -> dict:
        return self._wait_for_status(
            settings,
            task_id,
            initial_order,
            "activation",
            success_statuses=self.ACTIVATION_SUCCESS_STATUSES,
            failure_statuses=self.ACTIVATION_FAILURE_STATUSES,
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

        if not self._wait_for_bandwidth_available(
            tron_client,
            receiver,
            bandwidth_required,
            "post-delegation",
        ):
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

    @staticmethod
    def _post_active_recheck_attempts() -> int:
        return max(
            int(getattr(config, "PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS", 3)),
            1,
        )

    @staticmethod
    def _post_active_recheck_sleep_sec() -> float:
        return max(
            float(getattr(config, "PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_SLEEP_SEC", 1.0)),
            0.0,
        )

    def _sleep_before_next_resource_check(self, attempt: int) -> None:
        if attempt + 1 >= self._post_active_recheck_attempts():
            return
        sleep_for = self._post_active_recheck_sleep_sec()
        if sleep_for > 0:
            time.sleep(sleep_for)

    def _wait_for_energy_available(
        self,
        tron_client,
        receiver: str,
        threshold: int,
        stage: str,
    ) -> bool:
        attempts = self._post_active_recheck_attempts()
        for attempt in range(attempts):
            available = self._get_available_energy(
                tron_client,
                receiver,
                f"{stage} attempt {attempt + 1}/{attempts}",
            )
            if available is not None:
                logger.info(
                    f"ProfeeX energy on-chain check: {receiver=} "
                    f"available={available} threshold={threshold} "
                    f"attempt={attempt + 1}/{attempts}"
                )
                if available >= threshold:
                    return True
            self._sleep_before_next_resource_check(attempt)
        return False

    def _wait_for_bandwidth_available(
        self,
        tron_client,
        receiver: str,
        bandwidth_required: int,
        stage: str,
    ) -> bool:
        attempts = self._post_active_recheck_attempts()
        for attempt in range(attempts):
            try:
                available = has_free_bw(
                    receiver,
                    bandwidth_required,
                    tron_client=tron_client,
                )
            except Exception:
                logger.exception(
                    "Unable to read ProfeeX receiver bandwidth during "
                    f"{stage} attempt {attempt + 1}/{attempts}: {receiver}"
                )
                available = False
            logger.info(
                f"ProfeeX bandwidth on-chain check: {receiver=} "
                f"available={available} required={bandwidth_required} "
                f"attempt={attempt + 1}/{attempts}"
            )
            if available:
                return True
            self._sleep_before_next_resource_check(attempt)
        return False

    def _wait_until_active(
        self, settings, task_id: str, initial_order: dict, resource_name: str
    ) -> dict | None:
        try:
            return self._wait_for_status(
                settings,
                task_id,
                initial_order,
                resource_name,
                success_statuses=self.SUCCESS_STATUSES,
                failure_statuses=self.FAILURE_STATUSES,
            )
        except ProfeeXOrderError:
            return None

    def _wait_for_status(
        self,
        settings,
        task_id: str,
        initial_order: dict,
        resource_name: str,
        *,
        success_statuses,
        failure_statuses,
    ) -> dict:
        deadline = time.monotonic() + settings.timeout_sec
        order = initial_order
        last_status = None
        should_sleep_before_poll = False

        while True:
            status = order.get("status")
            if status != last_status:
                logger.info(f"ProfeeX {resource_name} order {task_id} status: {status}")
                last_status = status
            if status in success_statuses:
                return order
            if status in failure_statuses:
                raise self._order_error_from_order(resource_name, order)
            if status not in self.PENDING_STATUSES:
                raise ProfeeXOrderError(
                    resource_name,
                    f"ProfeeX {resource_name} order {task_id} returned unexpected status: {status}",
                    "UNKNOWN_ERROR",
                    temporary=False,
                )

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

            try:
                response = requests.get(
                    self._url(settings, f"/delegation/status/{task_id}"),
                    headers=self._headers(settings),
                    timeout=min(self.REQUEST_TIMEOUT_SEC, remaining),
                )
            except requests.RequestException as exc:
                logger.warning(
                    f"ProfeeX poll request failed for {resource_name} order {task_id}: {exc}"
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
            order = self._json_response(response, f"{resource_name} poll")
            should_sleep_before_poll = True

        raise ProfeeXOrderError(
            resource_name,
            f"ProfeeX {resource_name} order {task_id} did not reach success within "
            f"{settings.timeout_sec} seconds",
            "REQUEST_TIMEOUT",
            temporary=True,
        )

    @staticmethod
    def _safe_json(response):
        try:
            data = response.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _json_response(self, response, resource_name: str) -> dict:
        data = self._safe_json(response)
        if data is None:
            raise ProfeeXOrderError(
                resource_name,
                f"ProfeeX {resource_name} response is not a JSON object",
                "UNKNOWN_ERROR",
                temporary=False,
            )
        return data

    @staticmethod
    def _error_code_from_payload(data):
        if not isinstance(data, dict):
            return None
        if isinstance(data.get("error_code"), str):
            return data["error_code"]
        detail = data.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("error_code"), str):
            return detail["error_code"]
        return None

    @staticmethod
    def _headers(settings) -> dict:
        return {"X-API-Key": settings.api_key.get_secret_value()}

    @staticmethod
    def _url(settings, path: str) -> str:
        return f"{settings.api_base_url.rstrip('/')}{path}"


ProfeeXBandwidthProvider = ProfeeXProvider

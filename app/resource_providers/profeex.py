from __future__ import annotations

from dataclasses import dataclass
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
ACTIVATION_TERMINAL_ERROR_CODES = VALIDATION_ERROR_CODES | {
    "CONFIGURATION_ERROR",
    "INSUFFICIENT_BALANCE",
    "UNKNOWN_ERROR",
}
ACTIVATION_RETRYABLE_HTTP_STATUS_CODES = {408, 429}
FALLBACK_ELIGIBLE_PROFEEX_CODES = {
    "NETWORK_ERROR",
    "REQUEST_TIMEOUT",
    "DNS_ERROR",
    "CONNECT_ERROR",
    "READ_ERROR",
    "SERVICE_UNAVAILABLE",
    "HTTP_408",
    "HTTP_429",
    "HTTP_500",
    "HTTP_502",
    "HTTP_503",
    "HTTP_504",
    "RATE_LIMIT_EXCEEDED",
    "INSUFFICIENT_BALANCE",
    "MALFORMED_PRE_ACCEPT_RESPONSE",
    "ORDER_TIMEOUT",
    "ORDER_FAILED",
    "ORDER_CANCELLED",
    "PROCESSING_FAILED",
    "RESOURCE_RECHECK_FAILED",
    "FIXED_BANDWIDTH_BELOW_REQUIRED",
}
NON_FALLBACK_PROFEEX_CODES = {
    "INVALID_ADDRESS",
    "INVALID_PARAMETERS",
    "AUTHORIZATION_ERROR",
    "IP_NOT_WHITELISTED",
    "CONFIGURATION_ERROR",
    "ACCEPTED_ORDER_WITHOUT_TASK_ID",
}
ACCEPTED_ORDER_TERMINAL_ERROR_CODES = {
    "INVALID_ADDRESS",
    "INVALID_PARAMETERS",
    "AUTHORIZATION_ERROR",
    "IP_NOT_WHITELISTED",
    "CONFIGURATION_ERROR",
}


@dataclass
class ProviderFailure:
    code: str
    temporary: bool
    fallback_eligible: bool
    order_accepted: bool = False
    task_id: str | None = None


def classify_profeex_failure(
    code: str,
    *,
    task_id: str | None = None,
    order_accepted: bool = False,
) -> ProviderFailure:
    if order_accepted:
        return ProviderFailure(
            code=code,
            temporary=code not in ACCEPTED_ORDER_TERMINAL_ERROR_CODES,
            fallback_eligible=False,
            order_accepted=True,
            task_id=task_id,
        )

    if code in FALLBACK_ELIGIBLE_PROFEEX_CODES:
        return ProviderFailure(
            code=code,
            temporary=True,
            fallback_eligible=True,
            order_accepted=order_accepted,
            task_id=task_id,
        )

    if code == "RESOURCE_READ_FAILED":
        return ProviderFailure(
            code=code,
            temporary=True,
            fallback_eligible=False,
            order_accepted=order_accepted,
            task_id=task_id,
        )

    if code in NON_FALLBACK_PROFEEX_CODES:
        return ProviderFailure(
            code=code,
            temporary=False,
            fallback_eligible=False,
            order_accepted=order_accepted,
            task_id=task_id,
        )

    return ProviderFailure(
        code=code,
        temporary=True,
        fallback_eligible=not order_accepted,
        order_accepted=order_accepted,
        task_id=task_id,
    )


class ProfeeXOrderError(RuntimeError):
    def __init__(
        self,
        resource_name,
        message,
        error_code=None,
        temporary=False,
        *,
        provider_failure: ProviderFailure | None = None,
        order_accepted: bool = False,
        task_id: str | None = None,
    ):
        super().__init__(message)
        self.resource_name = resource_name
        self.error_code = error_code
        self.temporary = temporary
        self.provider_failure = provider_failure or classify_profeex_failure(
            error_code or "UNKNOWN_ERROR",
            task_id=task_id,
            order_accepted=order_accepted,
        )


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
        self.last_failure: ProviderFailure | None = None

    def _set_failure(
        self,
        code: str,
        *,
        task_id: str | None = None,
        order_accepted: bool = False,
    ) -> ProviderFailure:
        self.last_failure = classify_profeex_failure(
            code,
            task_id=task_id,
            order_accepted=order_accepted,
        )
        return self.last_failure

    def _set_failure_from_error(self, exc: ProfeeXOrderError) -> ProviderFailure:
        self.last_failure = exc.provider_failure
        return self.last_failure

    def acquire_energy(
        self,
        receiver: str,
        energy_to_provision: int,
        account_resource: dict,
        *,
        minimum_energy_required: int | None = None,
        strict_minimum_required: bool = False,
    ) -> bool:
        self.last_failure = None
        settings = config.PROFEEX
        if settings is None:
            self._set_failure("CONFIGURATION_ERROR")
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
            if (
                self.last_failure is not None
                and self.last_failure.code == "RESOURCE_READ_FAILED"
            ):
                self._set_failure(
                    "RESOURCE_READ_FAILED",
                    task_id=task_id,
                    order_accepted=True,
                )
            else:
                self._set_failure(
                    "RESOURCE_RECHECK_FAILED",
                    task_id=task_id,
                    order_accepted=True,
                )
            logger.warning(
                "Onetime account has not enough energy after ProfeeX delegation. "
                "Terminating transfer."
            )
            return False

        self.last_failure = None
        logger.info(f"ProfeeX energy successfully delegated: {active_order}")
        return True

    def release_energy(self, receiver: str) -> None:
        logger.info(
            f"ProfeeX energy for {receiver} returns after rent expiration. "
            "Skipping undelegate."
        )

    def activate_address(self, receiver: str) -> dict:
        self.last_failure = None
        settings = config.PROFEEX
        if settings is None:
            self._set_failure("CONFIGURATION_ERROR")
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
            code = self._request_exception_code(exc)
            provider_failure = self._set_failure(code)
            raise ProfeeXOrderError(
                "activation",
                f"ProfeeX activation request failed: {exc}",
                "SERVICE_UNAVAILABLE",
                temporary=True,
                provider_failure=provider_failure,
            ) from exc

        if response.status_code == 202:
            data = self._safe_json(response)
            if data is None:
                provider_failure = self._set_failure(
                    "ACCEPTED_ORDER_WITHOUT_TASK_ID",
                    order_accepted=True,
                )
                raise ProfeeXOrderError(
                    "activation",
                    "ProfeeX activation response is not a JSON object",
                    "ACCEPTED_ORDER_WITHOUT_TASK_ID",
                    temporary=True,
                    provider_failure=provider_failure,
                )
            task_id = self._extract_task_id(data, "activation")
            if task_id is None:
                raise ProfeeXOrderError(
                    "activation",
                    f"ProfeeX activation response has no task_id: {data}",
                    "ACCEPTED_ORDER_WITHOUT_TASK_ID",
                    temporary=True,
                    provider_failure=self.last_failure,
                )
            logger.info(f"ProfeeX activation accepted: {data}")
            self.last_failure = None
            return data

        if response.status_code == 409:
            raise ProfeeXOrderError(
                "activation",
                f"ProfeeX activation duplicate or already active: {response.text}",
                "DUPLICATE_REQUEST",
                temporary=True,
            )
        if response.status_code == 503:
            provider_failure = self._set_failure("SERVICE_UNAVAILABLE")
            raise ProfeeXOrderError(
                "activation",
                f"ProfeeX activation unavailable: {response.text}",
                "SERVICE_UNAVAILABLE",
                temporary=True,
                provider_failure=provider_failure,
            )

        data = self._safe_json(response)
        code = self._error_code_from_payload(data)
        if code is None:
            code = self._status_failure_code(response.status_code)
        temporary = self._activation_error_is_temporary(response.status_code, code)
        provider_failure = self._set_failure(code)
        public_code = self._activation_public_error_code(
            response.status_code,
            data,
            code,
        )
        raise ProfeeXOrderError(
            "activation",
            f"ProfeeX activation rejected with status {response.status_code}: {response.text}",
            public_code,
            temporary=temporary,
            provider_failure=provider_failure,
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
        self.last_failure = None
        settings = config.PROFEEX
        if settings is None:
            self._set_failure("CONFIGURATION_ERROR")
            logger.warning("PROFEEX config is missing. Cannot estimate USDT fee.")
            return None

        try:
            response = requests.get(
                self._url(settings, "/delegation/fee"),
                params={"receiver_address": receiver_address},
                headers=self._headers(settings),
                timeout=self.REQUEST_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            self._set_failure(self._request_exception_code(exc))
            logger.exception("ProfeeX USDT fee estimate request failed")
            return None

        if response.status_code != 200:
            self._set_failure(self._response_failure_code(response))
            logger.warning(
                f"ProfeeX USDT fee estimate rejected with status "
                f"{response.status_code}: {response.text}"
            )
            return None

        try:
            data = response.json()
        except ValueError:
            self._set_failure("MALFORMED_PRE_ACCEPT_RESPONSE")
            logger.exception("ProfeeX USDT fee estimate response is not valid JSON")
            return None
        if not isinstance(data, dict):
            self._set_failure("MALFORMED_PRE_ACCEPT_RESPONSE")
            logger.warning(
                f"ProfeeX USDT fee estimate response is not an object: {data}"
            )
            return None
        if type(data.get("energy_required")) is not int:
            self._set_failure("MALFORMED_PRE_ACCEPT_RESPONSE")
            logger.warning(f"ProfeeX USDT fee estimate has no energy_required: {data}")
            return None
        if data["energy_required"] < 0:
            self._set_failure("MALFORMED_PRE_ACCEPT_RESPONSE")
            logger.warning(
                f"ProfeeX USDT fee estimate has invalid energy_required: {data}"
            )
            return None
        if type(data.get("is_new_address")) is not bool:
            self._set_failure("MALFORMED_PRE_ACCEPT_RESPONSE")
            logger.warning(f"ProfeeX USDT fee estimate has no is_new_address flag: {data}")
            return None
        if "trx_burned" not in data:
            self._set_failure("MALFORMED_PRE_ACCEPT_RESPONSE")
            logger.warning(f"ProfeeX USDT fee estimate has no trx_burned field: {data}")
            return None
        self.last_failure = None
        return data

    def acquire_bandwidth(self, receiver: str, bandwidth_required: int) -> bool:
        self.last_failure = None
        settings = config.PROFEEX
        if settings is None:
            self._set_failure("CONFIGURATION_ERROR")
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
            self._set_failure("FIXED_BANDWIDTH_BELOW_REQUIRED")
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
            if (
                self.last_failure is not None
                and self.last_failure.code == "RESOURCE_READ_FAILED"
            ):
                self._set_failure(
                    "RESOURCE_READ_FAILED",
                    task_id=task_id,
                    order_accepted=True,
                )
            else:
                self._set_failure(
                    "RESOURCE_RECHECK_FAILED",
                    task_id=task_id,
                    order_accepted=True,
                )
            logger.warning(
                "Onetime account has not enough bandwidth after ProfeeX delegation. "
                "Terminating transfer."
            )
            return False

        self.last_failure = None
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
        except requests.RequestException as exc:
            self._set_failure(self._request_exception_code(exc))
            logger.exception(f"ProfeeX create {resource_name} order request failed")
            return None

        if response.status_code != 202:
            self._set_failure(self._response_failure_code(response))
            logger.warning(
                f"ProfeeX create {resource_name} order rejected with status "
                f"{response.status_code}: {response.text}"
            )
            return None

        try:
            data = response.json()
        except ValueError:
            self._set_failure(
                "ACCEPTED_ORDER_WITHOUT_TASK_ID",
                order_accepted=True,
            )
            logger.exception(
                f"ProfeeX create {resource_name} order response is not valid JSON"
            )
            return None
        if not isinstance(data, dict):
            self._set_failure(
                "ACCEPTED_ORDER_WITHOUT_TASK_ID",
                order_accepted=True,
            )
            logger.warning(
                f"ProfeeX create {resource_name} order response is not an object: {data}"
            )
            return None

        logger.info(f"ProfeeX {resource_name} order accepted: {data}")
        return data

    def _extract_task_id(self, order: dict, resource_name: str) -> str | None:
        task_id = order.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            self._set_failure(
                "ACCEPTED_ORDER_WITHOUT_TASK_ID",
                order_accepted=True,
            )
            logger.warning(
                f"ProfeeX {resource_name} order response has no task_id: {order}"
            )
            return None
        if "status" not in order:
            order["status"] = "PENDING"
        return task_id

    def _order_error_from_order(
        self,
        resource_name: str,
        order: dict,
        *,
        task_id: str | None = None,
    ) -> ProfeeXOrderError:
        error_code = order.get("error_code")
        if not isinstance(error_code, str) or not error_code:
            error_code = self._terminal_status_error_code(order.get("status"))
        raw_details = order.get("details")
        details = raw_details if isinstance(raw_details, dict) else {}
        message = details.get("error_message")
        if not message and isinstance(order.get("message"), str):
            message = order["message"]
        if not message and isinstance(order.get("detail"), str):
            message = order["detail"]
        if not message and isinstance(raw_details, str):
            message = raw_details
        if not message:
            message = f"ProfeeX {resource_name} order failed: {order}"
        provider_failure = classify_profeex_failure(
            error_code,
            order_accepted=task_id is not None,
            task_id=task_id,
        )
        return ProfeeXOrderError(
            resource_name,
            message,
            error_code,
            provider_failure.temporary,
            provider_failure=provider_failure,
        )

    def _get_available_energy(self, tron_client, receiver: str, stage: str) -> int | None:
        try:
            return get_available_energy(tron_client.get_account_resource(receiver))
        except Exception:
            self._set_failure("RESOURCE_READ_FAILED")
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
                self._set_failure("RESOURCE_READ_FAILED")
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
        except ProfeeXOrderError as exc:
            self._set_failure_from_error(exc)
            logger.warning(f"ProfeeX {resource_name} order {task_id} failed: {exc}")
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
                self.last_failure = None
                return order
            if status in failure_statuses:
                raise self._order_error_from_order(
                    resource_name,
                    order,
                    task_id=task_id,
                )
            if status not in self.PENDING_STATUSES:
                raise ProfeeXOrderError(
                    resource_name,
                    f"ProfeeX {resource_name} order {task_id} returned unexpected status: {status}",
                    "UNKNOWN_ERROR",
                    temporary=False,
                    order_accepted=True,
                    task_id=task_id,
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
                self._set_failure(
                    "POLL_TEMPORARY_ERROR",
                    task_id=task_id,
                    order_accepted=True,
                )
                logger.warning(
                    f"ProfeeX poll request failed for {resource_name} order {task_id}: {exc}"
                )
                should_sleep_before_poll = True
                continue

            if response.status_code != 200:
                self._set_failure(
                    "POLL_TEMPORARY_ERROR",
                    task_id=task_id,
                    order_accepted=True,
                )
                logger.warning(
                    f"ProfeeX poll for {resource_name} order {task_id} returned "
                    f"status {response.status_code}: {response.text}"
                )
                should_sleep_before_poll = True
                continue
            try:
                order = self._json_response(response, f"{resource_name} poll")
            except ProfeeXOrderError:
                self._set_failure(
                    "POLL_TEMPORARY_ERROR",
                    task_id=task_id,
                    order_accepted=True,
                )
                logger.warning(
                    f"ProfeeX poll for {resource_name} order {task_id} returned "
                    f"malformed response: {response.text}"
                )
                should_sleep_before_poll = True
                continue
            should_sleep_before_poll = True

        provider_failure = classify_profeex_failure(
            "ORDER_TIMEOUT",
            task_id=task_id,
            order_accepted=True,
        )
        raise ProfeeXOrderError(
            resource_name,
            f"ProfeeX {resource_name} order {task_id} did not reach success within "
            f"{settings.timeout_sec} seconds",
            "ORDER_TIMEOUT",
            temporary=True,
            provider_failure=provider_failure,
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
    def _activation_error_is_temporary(status_code, error_code):
        if error_code in TEMPORARY_ERROR_CODES:
            return True
        if error_code in ACTIVATION_TERMINAL_ERROR_CODES:
            return False
        if status_code in ACTIVATION_RETRYABLE_HTTP_STATUS_CODES:
            return True
        if 500 <= status_code <= 599:
            return True
        return False

    @staticmethod
    def _activation_public_error_code(status_code, data, provider_code):
        if ProfeeXProvider._error_code_from_payload(data) is not None:
            return provider_code
        if status_code == 500:
            return "UNKNOWN_ERROR"
        return provider_code

    @staticmethod
    def _request_exception_code(exc) -> str:
        if isinstance(exc, requests.ReadTimeout):
            return "READ_ERROR"
        if isinstance(exc, requests.ConnectTimeout):
            return "CONNECT_ERROR"
        if isinstance(exc, requests.Timeout):
            return "REQUEST_TIMEOUT"
        if isinstance(exc, requests.ConnectionError):
            return "CONNECT_ERROR"
        return "NETWORK_ERROR"

    @classmethod
    def _response_failure_code(cls, response) -> str:
        data = cls._safe_json(response)
        code = cls._error_code_from_payload(data)
        return code or cls._status_failure_code(response.status_code)

    @staticmethod
    def _status_failure_code(status_code) -> str:
        if status_code == 408:
            return "HTTP_408"
        if status_code == 429:
            return "HTTP_429"
        if status_code == 401:
            return "AUTHORIZATION_ERROR"
        if status_code == 403:
            return "IP_NOT_WHITELISTED"
        if status_code == 400 or status_code == 422:
            return "INVALID_PARAMETERS"
        if status_code in {500, 502, 503, 504}:
            return f"HTTP_{status_code}"
        if 500 <= status_code <= 599:
            return "SERVICE_UNAVAILABLE"
        return "UNKNOWN_ERROR"

    @staticmethod
    def _terminal_status_error_code(status) -> str:
        if status == "CANCELLED":
            return "ORDER_CANCELLED"
        if status == "FAILED":
            return "ORDER_FAILED"
        return "UNKNOWN_ERROR"

    @staticmethod
    def _headers(settings) -> dict:
        return {"X-API-Key": settings.api_key.get_secret_value()}

    @staticmethod
    def _url(settings, path: str) -> str:
        return f"{settings.api_base_url.rstrip('/')}{path}"


ProfeeXBandwidthProvider = ProfeeXProvider

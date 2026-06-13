from __future__ import annotations

from dataclasses import dataclass
import time
from decimal import Decimal, ROUND_CEILING

import requests

from .base import BandwidthProvider, EnergyProvider
from ..config import config
from ..connection_manager import ConnectionManager
from ..logging import logger
from ..utils import get_available_energy, has_free_bw


class RefeeProviderError(RuntimeError):
    def __init__(self, resource_name, message, error_code=None, temporary=False):
        super().__init__(message)
        self.resource_name = resource_name
        self.error_code = error_code
        self.temporary = temporary


@dataclass
class RefeeProviderFailure:
    code: str
    temporary: bool
    fallback_eligible: bool
    order_accepted: bool = False
    task_id: str | None = None


class RefeeProvider(EnergyProvider, BandwidthProvider):
    REQUEST_TIMEOUT_SEC = 10
    SUCCESS_STATUSES = {"delegated"}
    FAILURE_STATUSES = {"failed", "insufficient_funds", "canceled", "completed"}
    FIXED_ENERGY_ORDER_TOLERANCE = 500

    def __init__(self, tron_client=None):
        self.tron_client = tron_client
        self.last_failure: RefeeProviderFailure | None = None

    def _set_failure(
        self,
        code: str,
        *,
        temporary: bool = True,
        fallback_eligible: bool = False,
        order_accepted: bool = False,
        task_id: str | None = None,
    ) -> RefeeProviderFailure:
        self.last_failure = RefeeProviderFailure(
            code=code,
            temporary=temporary,
            fallback_eligible=fallback_eligible,
            order_accepted=order_accepted,
            task_id=task_id,
        )
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
        settings = config.REFEE
        if settings is None:
            self._set_failure(
                "CONFIGURATION_ERROR",
                temporary=False,
                fallback_eligible=False,
            )
            logger.warning("REFEE config is missing. Terminating transfer.")
            return False

        estimated_energy_required = (
            energy_to_provision
            if minimum_energy_required is None
            else minimum_energy_required
        )
        fixed_order_amount = getattr(config, "REFEE_FIXED_ENERGY_ORDER_AMOUNT", 0)
        if fixed_order_amount > 0:
            energy_required = max(
                fixed_order_amount - self.FIXED_ENERGY_ORDER_TOLERANCE,
                0,
            )
            if strict_minimum_required and minimum_energy_required is not None:
                energy_required = max(energy_required, minimum_energy_required)
        else:
            energy_required = estimated_energy_required
        if fixed_order_amount > 0:
            logger.info(
                "Using fixed re:Fee energy order amount: "
                f"{fixed_order_amount} energy; "
                f"estimated requirement was {estimated_energy_required}; "
                f"post-check threshold is {energy_required}"
            )
        tron_client = self.tron_client or ConnectionManager.client()
        onetime_energy_available = self._get_available_energy(
            tron_client, receiver, "pre-order"
        )
        if onetime_energy_available is None:
            self._set_failure("RESOURCE_READ_FAILED")
            return False
        if onetime_energy_available >= energy_required:
            logger.info(
                f"re:Fee order not needed for {receiver}: "
                f"{onetime_energy_available=} {energy_required=}"
            )
            return True

        if fixed_order_amount > 0:
            requested_amount = fixed_order_amount
            if strict_minimum_required and minimum_energy_required is not None:
                missing_for_minimum = max(
                    minimum_energy_required - onetime_energy_available,
                    0,
                )
                requested_amount = max(requested_amount, missing_for_minimum)
        else:
            energy_to_provision = energy_required - onetime_energy_available
            requested_amount = int(
                (
                    Decimal(energy_to_provision) * settings.energy_overprovision_factor
                ).to_integral_value(rounding=ROUND_CEILING)
            )
        amount = max(requested_amount, settings.min_energy_order_amount)
        logger.info(
            f"Requesting re:Fee energy rental for {receiver}: "
            f"{amount} energy for {settings.rent_duration_label}"
        )

        order = self._create_order(settings, receiver, amount)
        if order is None:
            return False

        order_id = order.get("id")
        if not order_id:
            self._set_failure(
                "ACCEPTED_ORDER_WITHOUT_ID",
                order_accepted=True,
            )
            logger.warning(f"re:Fee order response has no id field: {order}")
            return False

        delegated_order = self._wait_until_delegated(settings, order_id, order)
        if delegated_order is None:
            if self.last_failure is None:
                self._set_failure(
                    "ORDER_NOT_DELEGATED",
                    order_accepted=True,
                    task_id=order_id,
                )
            return False

        onetime_energy_available = self._get_available_energy(
            tron_client, receiver, "post-delegation"
        )
        if onetime_energy_available is None:
            self._set_failure(
                "RESOURCE_READ_FAILED",
                order_accepted=True,
                task_id=order_id,
            )
            return False
        logger.info(
            f"re:Fee on-chain check: {receiver=} "
            f"{onetime_energy_available=} {energy_required=}"
        )
        if onetime_energy_available < energy_required:
            self._set_failure(
                "RESOURCE_RECHECK_FAILED",
                order_accepted=True,
                task_id=order_id,
            )
            logger.warning(
                "Onetime account has not enough energy after re:Fee delegation. "
                "Terminating transfer."
            )
            return False

        self.last_failure = None
        logger.info(f"re:Fee energy successfully delegated: {delegated_order}")
        return True

    def release_energy(self, receiver: str) -> None:
        logger.info(
            f"re:Fee energy for {receiver} returns after rent expiration. "
            "Skipping undelegate."
        )

    def activate_address(self, destination: str) -> dict | None:
        settings = config.REFEE
        if settings is None:
            raise RefeeProviderError(
                "activation",
                "REFEE config is missing. Cannot activate destination.",
                "CONFIGURATION_ERROR",
                temporary=False,
            )
        try:
            response = requests.post(
                self._url(settings, "/api/functions/activate"),
                params={"address": destination},
                headers=self._headers(settings),
                timeout=self.REQUEST_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            raise RefeeProviderError(
                "activation",
                f"re:Fee activation request failed: {exc}",
                "SERVICE_UNAVAILABLE",
                temporary=True,
            ) from exc

        if response.status_code == 400:
            return {"status": "already_active", "address": destination}
        if response.status_code == 402:
            raise RefeeProviderError(
                "activation",
                f"re:Fee activation balance is insufficient: {response.text}",
                "INSUFFICIENT_BALANCE",
                temporary=True,
            )
        if response.status_code in {401, 403, 422}:
            raise RefeeProviderError(
                "activation",
                f"re:Fee activation configuration error: {response.text}",
                "CONFIGURATION_ERROR",
                temporary=False,
            )
        if (
            response.status_code in {408, 429}
            or 500 <= response.status_code <= 599
        ):
            raise RefeeProviderError(
                "activation",
                f"re:Fee activation unavailable: {response.text}",
                "SERVICE_UNAVAILABLE",
                temporary=True,
            )
        if response.status_code != 200:
            raise RefeeProviderError(
                "activation",
                f"re:Fee activation rejected with status {response.status_code}: {response.text}",
                "UNKNOWN_ERROR",
                temporary=True,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise RefeeProviderError(
                "activation",
                "re:Fee activation response is not valid JSON",
                "SCHEMA_ERROR",
                temporary=False,
            ) from exc
        if not isinstance(data, dict) or not data.get("txn_hash"):
            raise RefeeProviderError(
                "activation",
                f"re:Fee activation response has no txn_hash: {data}",
                "SCHEMA_ERROR",
                temporary=False,
            )
        return data

    def estimate_usdt_transfer_fee(self, source_address: str) -> dict | None:
        settings = config.REFEE
        if settings is None:
            logger.warning("REFEE config is missing. Cannot estimate USDT fee.")
            return None
        try:
            response = requests.get(
                self._url(settings, f"/api/functions/cost/{source_address}"),
                headers=self._headers(settings),
                timeout=self.REQUEST_TIMEOUT_SEC,
            )
        except requests.RequestException:
            logger.exception("re:Fee USDT fee estimate request failed")
            return None
        if response.status_code != 200:
            logger.warning(
                f"re:Fee USDT fee estimate rejected with status "
                f"{response.status_code}: {response.text}"
            )
            return None
        try:
            data = response.json()
        except ValueError:
            logger.exception("re:Fee USDT fee estimate response is not valid JSON")
            return None
        if (
            not isinstance(data, dict)
            or type(data.get("cost")) is not int
            or data["cost"] <= 0
        ):
            logger.warning(f"re:Fee USDT fee estimate has invalid cost: {data}")
            return None
        return {
            "energy_required": data["cost"],
            "is_new_address": False,
            "trx_burned": None,
            "provider": "refee",
        }

    def acquire_bandwidth(self, receiver: str, bandwidth_required: int) -> bool:
        self.last_failure = None
        settings = config.REFEE
        if settings is None:
            self._set_failure(
                "CONFIGURATION_ERROR",
                temporary=False,
                fallback_eligible=False,
            )
            logger.warning("REFEE config is missing. Terminating transfer.")
            return False

        tron_client = self.tron_client or ConnectionManager.client()
        if has_free_bw(receiver, bandwidth_required, tron_client=tron_client):
            logger.info(
                f"re:Fee bandwidth order not needed for {receiver}: "
                f"{bandwidth_required=} already available"
            )
            return True

        min_bandwidth_order_amount = getattr(
            settings, "min_bandwidth_order_amount", bandwidth_required
        )
        amount = max(bandwidth_required, min_bandwidth_order_amount)
        duration_label = getattr(
            settings, "bandwidth_rent_duration_label", settings.rent_duration_label
        )
        logger.info(
            f"Requesting re:Fee bandwidth rental for {receiver}: "
            f"{amount} bandwidth for {duration_label}"
        )

        order = self._create_order(
            settings,
            receiver,
            amount,
            resource="bandwidth",
            duration_label=duration_label,
        )
        if order is None:
            return False

        order_id = order.get("id")
        if not order_id:
            self._set_failure(
                "ACCEPTED_ORDER_WITHOUT_ID",
                order_accepted=True,
            )
            logger.warning(f"re:Fee bandwidth order response has no id field: {order}")
            return False

        delegated_order = self._wait_until_delegated(settings, order_id, order)
        if delegated_order is None:
            if self.last_failure is None:
                self._set_failure(
                    "ORDER_NOT_DELEGATED",
                    order_accepted=True,
                    task_id=order_id,
                )
            return False

        try:
            bandwidth_ready = has_free_bw(
                receiver,
                bandwidth_required,
                tron_client=tron_client,
            )
        except Exception:
            self._set_failure(
                "RESOURCE_READ_FAILED",
                order_accepted=True,
                task_id=order_id,
            )
            logger.exception(
                "Failed to read bandwidth after re:Fee delegation for %s",
                receiver,
            )
            return False

        if not bandwidth_ready:
            self._set_failure(
                "RESOURCE_RECHECK_FAILED",
                order_accepted=True,
                task_id=order_id,
            )
            logger.warning(
                "Onetime account has not enough bandwidth after re:Fee delegation. "
                "Terminating transfer."
            )
            return False

        self.last_failure = None
        logger.info(f"re:Fee bandwidth successfully delegated: {delegated_order}")
        return True

    def _create_order(
        self,
        settings,
        receiver: str,
        amount: int,
        resource: str = "energy",
        duration_label: str | None = None,
    ) -> dict | None:
        url = self._url(settings, "/api/rent_resource/orders")
        payload = {
            "address": receiver,
            "amount": amount,
            "resource": resource,
            "duration_label": duration_label or settings.rent_duration_label,
        }
        try:
            response = requests.post(
                url,
                json=payload,
                headers=self._headers(settings),
                timeout=self.REQUEST_TIMEOUT_SEC,
            )
        except requests.RequestException:
            self._set_failure(
                "SERVICE_UNAVAILABLE",
                temporary=True,
                fallback_eligible=True,
            )
            logger.exception("re:Fee create order request failed")
            return None

        if response.status_code != 202:
            self._set_create_order_http_failure(response.status_code)
            logger.warning(
                f"re:Fee create order rejected with status "
                f"{response.status_code}: {response.text}"
            )
            return None

        try:
            data = response.json()
        except ValueError:
            self._set_failure(
                "ACCEPTED_MALFORMED_RESPONSE",
                temporary=True,
                fallback_eligible=False,
                order_accepted=True,
            )
            logger.exception("re:Fee create order response is not valid JSON")
            return None
        if not isinstance(data, dict):
            self._set_failure(
                "ACCEPTED_MALFORMED_RESPONSE",
                temporary=True,
                fallback_eligible=False,
                order_accepted=True,
            )
            logger.warning(f"re:Fee create order response is not an object: {data}")
            return None

        logger.info(f"re:Fee order accepted: {data}")
        return data

    def _set_create_order_http_failure(self, status_code: int) -> None:
        if status_code == 400:
            self._set_failure(
                "INVALID_PARAMETERS",
                temporary=False,
                fallback_eligible=False,
            )
            return
        if status_code == 402:
            self._set_failure(
                "INSUFFICIENT_BALANCE",
                temporary=True,
                fallback_eligible=False,
            )
            return
        if status_code in {401, 403, 422}:
            self._set_failure(
                "CONFIGURATION_ERROR",
                temporary=False,
                fallback_eligible=False,
            )
            return
        if status_code in {408, 429} or 500 <= status_code <= 599:
            self._set_failure(
                "SERVICE_UNAVAILABLE",
                temporary=True,
                fallback_eligible=True,
            )
            return
        self._set_failure(
            "UNKNOWN_ERROR",
            temporary=True,
            fallback_eligible=False,
        )

    def _wait_until_delegated(
        self, settings, order_id: str, initial_order: dict
    ) -> dict | None:
        deadline = time.monotonic() + settings.timeout_sec
        order = initial_order
        last_status = None

        while time.monotonic() <= deadline:
            status = order.get("status")
            if status != last_status:
                logger.info(f"re:Fee order {order_id} status: {status}")
                last_status = status

            if status in self.SUCCESS_STATUSES:
                return order
            if status in self.FAILURE_STATUSES:
                self._set_failure(
                    "ORDER_FAILED",
                    order_accepted=True,
                    task_id=order_id,
                )
                logger.warning(f"re:Fee order {order_id} failed: {order}")
                return None

            time.sleep(settings.poll_interval_sec)

            try:
                response = requests.get(
                    self._url(settings, f"/api/rent_resource/orders/{order_id}"),
                    headers=self._headers(settings),
                    timeout=self.REQUEST_TIMEOUT_SEC,
                )
            except requests.RequestException:
                logger.warning(f"re:Fee poll request failed for order {order_id}")
                continue

            if response.status_code != 200:
                logger.warning(
                    f"re:Fee poll for order {order_id} returned status "
                    f"{response.status_code}: {response.text}"
                )
                continue

            try:
                order = response.json()
            except ValueError:
                self._set_failure(
                    "POLL_MALFORMED_RESPONSE",
                    order_accepted=True,
                    task_id=order_id,
                )
                logger.exception(f"re:Fee poll response is not valid JSON: {order_id}")
                return None
            if not isinstance(order, dict):
                self._set_failure(
                    "POLL_MALFORMED_RESPONSE",
                    order_accepted=True,
                    task_id=order_id,
                )
                logger.warning(
                    f"re:Fee poll response is not an object for order {order_id}: {order}"
                )
                return None

        self._set_failure(
            "ORDER_TIMEOUT",
            order_accepted=True,
            task_id=order_id,
        )
        logger.warning(
            f"re:Fee order {order_id} did not reach delegated status within "
            f"{settings.timeout_sec} seconds"
        )
        return None

    @staticmethod
    def _headers(settings) -> dict:
        return {"X-API-Key": settings.api_key.get_secret_value()}

    @staticmethod
    def _url(settings, path: str) -> str:
        return f"{settings.api_base_url.rstrip('/')}{path}"

    @staticmethod
    def _get_available_energy(tron_client, receiver: str, check_name: str) -> int | None:
        try:
            account_resource = tron_client.get_account_resource(receiver)
        except Exception:
            logger.exception(f"re:Fee {check_name} resource check failed")
            return None
        return get_available_energy(account_resource)

import time
from decimal import Decimal, ROUND_CEILING

import requests

from .base import BandwidthProvider, EnergyProvider
from ..config import config
from ..connection_manager import ConnectionManager
from ..logging import logger
from ..utils import get_available_energy, has_free_bw


class RefeeProvider(EnergyProvider, BandwidthProvider):
    REQUEST_TIMEOUT_SEC = 10
    SUCCESS_STATUSES = {"delegated"}
    FAILURE_STATUSES = {"failed", "insufficient_funds", "canceled", "completed"}

    def __init__(self, tron_client=None):
        self.tron_client = tron_client

    def acquire_energy(
        self,
        receiver: str,
        energy_to_provision: int,
        account_resource: dict,
        *,
        minimum_energy_required: int | None = None,
    ) -> bool:
        settings = config.REFEE
        if settings is None:
            logger.warning("REFEE config is missing. Terminating transfer.")
            return False

        estimated_energy_required = (
            energy_to_provision
            if minimum_energy_required is None
            else minimum_energy_required
        )
        fixed_order_amount = getattr(config, "REFEE_FIXED_ENERGY_ORDER_AMOUNT", 0)
        energy_required = (
            fixed_order_amount if fixed_order_amount > 0 else estimated_energy_required
        )
        if fixed_order_amount > 0:
            logger.info(
                "Using fixed re:Fee energy order amount: "
                f"{fixed_order_amount} energy; "
                f"estimated requirement was {estimated_energy_required}"
            )
        tron_client = self.tron_client or ConnectionManager.client()
        onetime_energy_available = self._get_available_energy(
            tron_client, receiver, "pre-order"
        )
        if onetime_energy_available is None:
            return False
        if onetime_energy_available >= energy_required:
            logger.info(
                f"re:Fee order not needed for {receiver}: "
                f"{onetime_energy_available=} {energy_required=}"
            )
            return True

        if fixed_order_amount > 0:
            requested_amount = fixed_order_amount
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
            logger.warning(f"re:Fee order response has no id field: {order}")
            return False

        delegated_order = self._wait_until_delegated(settings, order_id, order)
        if delegated_order is None:
            return False

        onetime_energy_available = self._get_available_energy(
            tron_client, receiver, "post-delegation"
        )
        if onetime_energy_available is None:
            return False
        logger.info(
            f"re:Fee on-chain check: {receiver=} "
            f"{onetime_energy_available=} {energy_required=}"
        )
        if onetime_energy_available < energy_required:
            logger.warning(
                "Onetime account has not enough energy after re:Fee delegation. "
                "Terminating transfer."
            )
            return False

        logger.info(f"re:Fee energy successfully delegated: {delegated_order}")
        return True

    def release_energy(self, receiver: str) -> None:
        logger.info(
            f"re:Fee energy for {receiver} returns after rent expiration. "
            "Skipping undelegate."
        )

    def acquire_bandwidth(self, receiver: str, bandwidth_required: int) -> bool:
        settings = config.REFEE
        if settings is None:
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
            logger.warning(f"re:Fee bandwidth order response has no id field: {order}")
            return False

        delegated_order = self._wait_until_delegated(settings, order_id, order)
        if delegated_order is None:
            return False

        if not has_free_bw(receiver, bandwidth_required, tron_client=tron_client):
            logger.warning(
                "Onetime account has not enough bandwidth after re:Fee delegation. "
                "Terminating transfer."
            )
            return False

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
            logger.exception("re:Fee create order request failed")
            return None

        if response.status_code != 202:
            logger.warning(
                f"re:Fee create order rejected with status "
                f"{response.status_code}: {response.text}"
            )
            return None

        try:
            data = response.json()
        except ValueError:
            logger.exception("re:Fee create order response is not valid JSON")
            return None
        if not isinstance(data, dict):
            logger.warning(f"re:Fee create order response is not an object: {data}")
            return None

        logger.info(f"re:Fee order accepted: {data}")
        return data

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
                logger.exception(f"re:Fee poll response is not valid JSON: {order_id}")
                return None
            if not isinstance(order, dict):
                logger.warning(
                    f"re:Fee poll response is not an object for order {order_id}: {order}"
                )
                return None

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

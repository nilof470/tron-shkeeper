import json
import math
import time
from abc import ABC, abstractmethod
from decimal import Decimal, ROUND_CEILING

import requests

from .config import config
from .connection_manager import ConnectionManager
from .logging import logger
from .utils import get_available_energy, get_energy_delegator


class EnergyProvider(ABC):
    """
    Abstract energy-acquisition strategy used by transfer_trc20_from.

    Phase 1 has a single concrete implementation (StakingEnergyProvider)
    that lifts the inline freeze-v2 / delegate-v2 logic out of
    transfer_trc20_from. Phase 2 will add RefeeEnergyProvider.
    """

    @abstractmethod
    def acquire(
        self,
        receiver: str,
        energy_to_provision: int,
        account_resource: dict,
        *,
        minimum_energy_required: int | None = None,
    ) -> bool:
        """
        Make `energy_to_provision` units of TRON ENERGY available on `receiver`.

        Args:
            receiver: TRON base58 address that needs energy (the onetime
                user-wallet about to broadcast a TRC-20 transfer).
            energy_to_provision: amount of energy units to provision in this
                call. For the additional-delegation path this is the missing
                delta, not the total required by the transfer.
            account_resource: the dict returned by
                tron_client.get_account_resource(receiver). Used to size
                the delegation in the staking case (TotalEnergyWeight /
                TotalEnergyLimit ratio).
            minimum_energy_required: total EnergyLimit that must be present
                after provisioning. Defaults to energy_to_provision. Plan 02
                passes the transfer's full energy_needed here for the top-up
                path so the lifted recheck remains behavior-identical to the
                original closure, which captured the outer energy_needed.

        Returns:
            True when provisioning completed and the post-check confirms
            enough energy; False when the caller should terminate the sweep.
        """

    @abstractmethod
    def release(self, receiver: str) -> None:
        """Release any provider-owned resources from `receiver`."""


class StakingEnergyProvider(EnergyProvider):
    def __init__(self, tron_client=None):
        self.tron_client = tron_client

    def acquire(
        self,
        receiver: str,
        energy_to_provision: int,
        account_resource: dict,
        *,
        minimum_energy_required: int | None = None,
    ) -> bool:
        tron_client = self.tron_client or ConnectionManager.client()
        energy_delegator_priv, energy_delegator_pub = get_energy_delegator()
        energy_needed = (
            energy_to_provision
            if minimum_energy_required is None
            else minimum_energy_required
        )
        sun_to_delegate = self._calc_sun_for_energy_delegation(
            energy_to_provision, account_resource
        )

        logger.info("Check if energy delegator account can delegate energy")
        result = tron_client.provider.make_request(
            "wallet/getcandelegatedmaxsize",
            {"owner_address": energy_delegator_pub, "type": 1, "visible": True},
        )
        if "max_size" not in result:
            logger.warning(
                "Energy delegator has no delegatable energy. Terminating transfer."
            )
            return False

        else:
            delegetable_sun = result["max_size"]

            logger.info(f"{delegetable_sun=} {sun_to_delegate=}")

            if delegetable_sun < sun_to_delegate:
                logger.warning(
                    "Energy delegator has not enough energy. Terminating transfer."
                )
                return False
            else:
                logger.info("Energy delegator has enough energy")

                logger.info("Delegating energy to onetime account")

                unsigned_tx = tron_client.trx.delegate_resource(
                    owner=energy_delegator_pub,
                    receiver=receiver,
                    balance=sun_to_delegate,
                    resource="ENERGY",
                ).build()
                signed_tx = unsigned_tx.sign(energy_delegator_priv)
                logger.info(f"TX json size: {len(json.dumps(signed_tx._raw_data))}")

                delegate_tx_info = signed_tx.broadcast().wait()

                logger.info(
                    f"Delegated {energy_needed} energy to onetime account {receiver} with TXID: {unsigned_tx.txid}"
                )
                logger.info(delegate_tx_info)

                logger.info(
                    "Recheck resources of the onetime address after energy delegation"
                )
                onetime_address_resources = tron_client.get_account_resource(receiver)
                onetime_energy_available = get_available_energy(
                    onetime_address_resources
                )
                logger.info(
                    f"{receiver=} {onetime_energy_available=} {energy_needed=}"
                )
                if onetime_energy_available < energy_needed:
                    logger.warning(
                        "Onetime account has not enough energy after delegation. Terminating transfer."
                    )
                    return False
                else:
                    logger.info("Energy successfuly delegated")
                    return True

    def release(self, receiver: str) -> None:
        from app.tasks import undelegate_energy

        if config.DEVMODE_CELERY_NODELAY:
            undelegate_energy(receiver)
        else:
            undelegate_energy.delay(receiver)

    @staticmethod
    def _calc_sun_for_energy_delegation(energy: int, res: dict) -> int:
        trx: int = math.ceil(
            (res["TotalEnergyWeight"] * energy) / res["TotalEnergyLimit"]
        )
        trx *= config.ENERGY_DELEGATION_MODE_ENERGY_DELEGATION_FACTOR
        return int(trx * 1_000_000)


class RefeeEnergyProvider(EnergyProvider):
    REQUEST_TIMEOUT_SEC = 10
    SUCCESS_STATUSES = {"delegated"}
    FAILURE_STATUSES = {"failed", "insufficient_funds", "canceled", "completed"}

    def __init__(self, tron_client=None):
        self.tron_client = tron_client

    def acquire(
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

        energy_required = (
            energy_to_provision
            if minimum_energy_required is None
            else minimum_energy_required
        )
        amount = int(
            (
                Decimal(energy_to_provision) * settings.energy_overprovision_factor
            ).to_integral_value(rounding=ROUND_CEILING)
        )
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

        tron_client = self.tron_client or ConnectionManager.client()
        try:
            onetime_address_resources = tron_client.get_account_resource(receiver)
        except Exception:
            logger.exception("re:Fee on-chain resource check failed")
            return False
        onetime_energy_available = get_available_energy(onetime_address_resources)
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

    def release(self, receiver: str) -> None:
        logger.info(
            f"re:Fee energy for {receiver} returns after rent expiration. "
            "Skipping undelegate."
        )

    def _create_order(self, settings, receiver: str, amount: int) -> dict | None:
        url = self._url(settings, "/api/rent_resource/orders")
        payload = {
            "address": receiver,
            "amount": amount,
            "resource": "energy",
            "duration_label": settings.rent_duration_label,
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
                logger.exception(f"re:Fee poll request failed for order {order_id}")
                return None

            if response.status_code != 200:
                logger.warning(
                    f"re:Fee poll for order {order_id} returned status "
                    f"{response.status_code}: {response.text}"
                )
                return None

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


def get_energy_provider(tron_client=None) -> EnergyProvider:
    if config.ENERGY_SOURCE == "refee":
        return RefeeEnergyProvider(tron_client=tron_client)
    return StakingEnergyProvider(tron_client=tron_client)

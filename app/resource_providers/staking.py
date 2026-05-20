import json
import math

from .base import EnergyProvider
from ..config import config
from ..connection_manager import ConnectionManager
from ..logging import logger
from ..utils import get_available_energy, get_energy_delegator


class StakingEnergyProvider(EnergyProvider):
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

    def release_energy(self, receiver: str) -> None:
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

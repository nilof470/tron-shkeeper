from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import time

import tronpy.keys

from app.config import config
from app.connection_manager import ConnectionManager
from app.logging import logger
from app.payout_destination_activation import (
    DestinationActivationError,
    ensure_destination_activated,
)
from app.resource_providers.factory import get_bandwidth_provider, get_energy_provider
from app.resource_providers.profeex import ProfeeXProvider
from app.schemas import KeyType
from app.utils import get_available_energy, get_key, has_free_bw


@dataclass
class ResourceReadiness:
    provider: str | None
    required: int
    available: int
    deficit: int


@dataclass
class PayoutResourceQuote:
    source_address: str
    destination: str
    amount: str
    activation_required: bool
    estimated_trx_burned: str | None
    energy: ResourceReadiness
    bandwidth: ResourceReadiness
    submit_ready: bool
    blocking_code: str | None
    blocking_reason: str | None

    def to_dict(self) -> dict:
        return asdict(self)


class PayoutResourceError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def get_available_bandwidth(account_resource: dict) -> int:
    staked = max(
        account_resource.get("NetLimit", 0) - account_resource.get("NetUsed", 0),
        0,
    )
    daily = max(
        account_resource.get("freeNetLimit", 0)
        - account_resource.get("freeNetUsed", 0),
        0,
    )
    return max(staked, daily)


def provider_label(provider, configured_name: str) -> str | None:
    if provider is None:
        return None
    return configured_name


def configured_energy_provider(tron_client):
    if config.ENERGY_PROVIDER == "staking":
        return None
    return get_energy_provider(tron_client=tron_client)


def estimate_usdt_transfer_fee_via_profeex(destination: str) -> dict | None:
    return ProfeeXProvider().estimate_usdt_transfer_fee(destination)


def _get_fee_deposit_resources(client, fee_deposit_address: str) -> dict:
    try:
        return client.get_account_resource(fee_deposit_address)
    except Exception as exc:
        raise PayoutResourceError(
            "Unable to read TRON fee-deposit account resources",
            code="RESOURCE_READ_FAILED",
        ) from exc


def estimate_fee_deposit_resources_for_usdt_payout(
    destination: str,
    amount: Decimal,
    *,
    tron_client=None,
) -> PayoutResourceQuote:
    tronpy.keys.to_base58check_address(destination)
    client = tron_client or ConnectionManager.client()
    _, fee_deposit_address = get_key(KeyType.fee_deposit)
    account_resource = _get_fee_deposit_resources(client, fee_deposit_address)

    fee_estimate = estimate_usdt_transfer_fee_via_profeex(destination)
    blocking_reason = None
    blocking_code = None
    activation_required = False
    estimated_trx_burned = None
    if fee_estimate is None:
        blocking_code = "PROFEEX_ESTIMATE_UNAVAILABLE"
        blocking_reason = "Unable to estimate TRON USDT transfer energy through ProfeeX"
        energy_required = 0
    else:
        energy_required = fee_estimate["energy_required"]
        activation_required = bool(fee_estimate.get("is_new_address"))
        trx_burned = fee_estimate.get("trx_burned")
        estimated_trx_burned = str(trx_burned) if trx_burned is not None else None
        if activation_required:
            blocking_code = "DESTINATION_NOT_ACTIVATED"
            blocking_reason = "TRON payout destination is not activated"

    energy_available = get_available_energy(account_resource)
    energy_deficit = max(energy_required - energy_available, 0)
    bandwidth_required = config.BANDWIDTH_PER_TRC20_TRANSFER_CALL
    bandwidth_available = get_available_bandwidth(account_resource)
    bandwidth_deficit = (
        0
        if has_free_bw(
            fee_deposit_address,
            bandwidth_required,
            tron_client=client,
        )
        else max(bandwidth_required - bandwidth_available, 0)
    )

    energy_provider = configured_energy_provider(client)
    bandwidth_provider = get_bandwidth_provider(tron_client=client)

    if blocking_reason is None and energy_deficit and energy_provider is None:
        blocking_code = "PROVIDER_UNAVAILABLE"
        blocking_reason = (
            "No energy provider is configured for TRON USDT payout resources"
        )
    elif blocking_reason is None and bandwidth_deficit and bandwidth_provider is None:
        blocking_code = "PROVIDER_UNAVAILABLE"
        blocking_reason = (
            "No bandwidth provider is configured for TRON USDT payout resources"
        )

    return PayoutResourceQuote(
        source_address=fee_deposit_address,
        destination=destination,
        amount=str(amount),
        activation_required=activation_required,
        estimated_trx_burned=estimated_trx_burned,
        energy=ResourceReadiness(
            provider=provider_label(energy_provider, config.ENERGY_PROVIDER),
            required=energy_required,
            available=energy_available,
            deficit=energy_deficit,
        ),
        bandwidth=ResourceReadiness(
            provider=provider_label(bandwidth_provider, config.BANDWIDTH_PROVIDER),
            required=bandwidth_required,
            available=bandwidth_available,
            deficit=bandwidth_deficit,
        ),
        submit_ready=blocking_reason is None,
        blocking_code=blocking_code,
        blocking_reason=blocking_reason,
    )


def ensure_fee_deposit_resources_for_usdt_payout(
    destination: str,
    amount: Decimal,
    *,
    tron_client=None,
    allow_destination_activation: bool = False,
) -> PayoutResourceQuote:
    client = tron_client or ConnectionManager.client()
    quote = estimate_fee_deposit_resources_for_usdt_payout(
        destination,
        amount,
        tron_client=client,
    )
    if (
        not quote.submit_ready
        and quote.blocking_code == "DESTINATION_NOT_ACTIVATED"
        and allow_destination_activation
        and config.TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION
    ):
        try:
            ensure_destination_activated(
                destination,
                quote_fn=lambda receiver: estimate_usdt_transfer_fee_via_profeex(
                    receiver
                ),
            )
        except DestinationActivationError as exc:
            raise PayoutResourceError(str(exc), code=exc.code) from exc
        quote = estimate_fee_deposit_resources_for_usdt_payout(
            destination,
            amount,
            tron_client=client,
        )
    if not quote.submit_ready:
        raise PayoutResourceError(
            quote.blocking_reason or "TRON USDT payout resources are not ready",
            code=quote.blocking_code,
        )

    provisioned = False
    if quote.energy.deficit:
        energy_provider = configured_energy_provider(client)
        if energy_provider is None:
            raise PayoutResourceError(
                "No energy provider is configured",
                code="PROVIDER_UNAVAILABLE",
            )
        account_resource = _get_fee_deposit_resources(client, quote.source_address)
        logger.info(
            f"Preparing TRON USDT payout energy for {quote.source_address}: "
            f"required={quote.energy.required} deficit={quote.energy.deficit}"
        )
        if not energy_provider.acquire_energy(
            quote.source_address,
            quote.energy.deficit,
            account_resource,
            minimum_energy_required=quote.energy.required,
            strict_minimum_required=True,
        ):
            raise PayoutResourceError(
                "Energy provider failed to prepare resources",
                code="PROVIDER_FAILED",
            )
        provisioned = True

    if quote.bandwidth.deficit:
        bandwidth_provider = get_bandwidth_provider(tron_client=client)
        if bandwidth_provider is None:
            raise PayoutResourceError(
                "No bandwidth provider is configured",
                code="PROVIDER_UNAVAILABLE",
            )
        logger.info(
            f"Preparing TRON USDT payout bandwidth for {quote.source_address}: "
            f"required={quote.bandwidth.required} deficit={quote.bandwidth.deficit}"
        )
        if not bandwidth_provider.acquire_bandwidth(
            quote.source_address,
            quote.bandwidth.required,
        ):
            raise PayoutResourceError(
                "Bandwidth provider failed to prepare resources",
                code="PROVIDER_FAILED",
            )
        provisioned = True

    if (
        not provisioned
        and quote.submit_ready
        and quote.energy.deficit == 0
        and quote.bandwidth.deficit == 0
    ):
        return quote

    for attempt in range(config.PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS):
        refreshed = estimate_fee_deposit_resources_for_usdt_payout(
            destination,
            amount,
            tron_client=client,
        )
        if (
            refreshed.submit_ready
            and refreshed.energy.deficit == 0
            and refreshed.bandwidth.deficit == 0
        ):
            return refreshed
        if attempt + 1 < config.PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS:
            time.sleep(config.PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_SLEEP_SEC)

    raise PayoutResourceError(
        refreshed.blocking_reason
        or "TRON USDT payout resources are still insufficient after provider provisioning",
        code=refreshed.blocking_code or "RESOURCE_RECHECK_FAILED",
    )

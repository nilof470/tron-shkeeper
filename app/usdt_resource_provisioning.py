from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import time

from app.config import config
from app.connection_manager import ConnectionManager
from app.logging import logger
from app.resource_providers.factory import (
    get_bandwidth_provider,
    get_bandwidth_provider_by_name,
    get_energy_provider,
    get_energy_provider_by_name,
)
from app.resource_providers.profeex import ProfeeXProvider
from app.resource_providers.refee import RefeeProvider
from app.utils import get_available_energy


TEMPORARY_RESOURCE_BLOCKING_CODES = {
    "RESOURCE_ESTIMATE_UNAVAILABLE",
    "RESOURCE_READ_FAILED",
    "RESOURCE_RECHECK_FAILED",
    "PROVIDER_FAILED",
}


@dataclass
class UsdtResourceReadiness:
    provider: str | None
    required: int
    available: int
    deficit: int


@dataclass
class UsdtResourceQuote:
    source_address: str
    destination: str
    amount: str
    estimate_provider: str | None
    activation_required: bool
    estimated_trx_burned: str | None
    energy: UsdtResourceReadiness
    bandwidth: UsdtResourceReadiness
    submit_ready: bool
    blocking_code: str | None
    blocking_reason: str | None

    def to_dict(self) -> dict:
        return asdict(self)


class UsdtResourceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        temporary: bool = False,
        provider_order_accepted: bool = False,
        provider_task_id: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.temporary = temporary
        self.provider_order_accepted = provider_order_accepted
        self.provider_task_id = provider_task_id


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


def _fallback_provider_name() -> str:
    return getattr(config, "TRON_USDT_RESOURCE_FALLBACK_PROVIDER", "disabled")


def _energy_provider_chain(tron_client):
    providers = []
    primary = config.ENERGY_PROVIDER
    if primary != "staking":
        providers.append((primary, get_energy_provider(tron_client)))
    fallback = _fallback_provider_name()
    if fallback != "disabled" and fallback != primary:
        providers.append((fallback, get_energy_provider_by_name(fallback, tron_client)))
    return [(name, provider) for name, provider in providers if provider is not None]


def _bandwidth_provider_chain(tron_client):
    providers = []
    primary = config.BANDWIDTH_PROVIDER
    if primary != "disabled":
        providers.append((primary, get_bandwidth_provider(tron_client)))
    fallback = _fallback_provider_name()
    if fallback != "disabled" and fallback != primary:
        providers.append(
            (fallback, get_bandwidth_provider_by_name(fallback, tron_client))
        )
    return [(name, provider) for name, provider in providers if provider is not None]


def _get_account_resources(client, source_address: str) -> dict:
    try:
        return client.get_account_resource(source_address)
    except Exception as exc:
        raise UsdtResourceError(
            "Unable to read TRON USDT source account resources",
            code="RESOURCE_READ_FAILED",
            temporary=True,
        ) from exc


def _provider_failure(provider):
    return getattr(provider, "last_failure", None)


def _provider_fallback_eligible(provider) -> bool:
    failure = _provider_failure(provider)
    if failure is None:
        return True
    return failure.fallback_eligible is True


def _raise_provider_failure(provider, resource_name: str) -> None:
    failure = _provider_failure(provider)
    if failure is None:
        raise UsdtResourceError(
            f"{resource_name} provider failed to prepare resources",
            code="PROVIDER_FAILED",
            temporary=True,
        )
    raise UsdtResourceError(
        f"{resource_name} provider failed to prepare resources: {failure.code}",
        code=failure.code,
        temporary=failure.temporary,
        provider_order_accepted=failure.order_accepted,
        provider_task_id=failure.task_id,
    )


def _bandwidth_ready(client, source_address: str, bandwidth_required: int) -> bool:
    account_resource = _get_account_resources(client, source_address)
    return get_available_bandwidth(account_resource) >= bandwidth_required


def estimate_usdt_transfer_fee_chain(
    source_address: str,
    destination: str,
    *,
    tron_client=None,
) -> tuple[str | None, dict | None, object | None]:
    if config.ENERGY_PROVIDER == "refee":
        estimate = RefeeProvider(tron_client=tron_client).estimate_usdt_transfer_fee(
            source_address
        )
        if estimate is not None:
            return "refee", estimate, None
        return None, None, None

    profeex_provider = ProfeeXProvider()
    estimate = profeex_provider.estimate_usdt_transfer_fee(destination)
    if estimate is not None:
        return "profeex", estimate, None

    profeex_failure = _provider_failure(profeex_provider)
    if not _provider_fallback_eligible(profeex_provider):
        return None, None, profeex_failure

    if _fallback_provider_name() == "refee":
        estimate = RefeeProvider(tron_client=tron_client).estimate_usdt_transfer_fee(
            source_address
        )
        if estimate is not None:
            return "refee", estimate, None

    return None, None, None


def estimate_usdt_transfer_resources(
    source_address: str,
    destination: str,
    amount: Decimal,
    *,
    tron_client=None,
) -> UsdtResourceQuote:
    client = tron_client or ConnectionManager.client()
    account_resource = _get_account_resources(client, source_address)
    estimate_provider, fee_estimate, estimate_failure = estimate_usdt_transfer_fee_chain(
        source_address,
        destination,
        tron_client=client,
    )
    return _quote_from_resources(
        source_address,
        destination,
        amount,
        account_resource,
        estimate_provider,
        fee_estimate,
        estimate_failure,
        tron_client=client,
    )


def ensure_usdt_transfer_resources(
    source_address: str,
    destination: str,
    amount: Decimal,
    *,
    tron_client=None,
) -> UsdtResourceQuote:
    client = tron_client or ConnectionManager.client()
    quote = estimate_usdt_transfer_resources(
        source_address,
        destination,
        amount,
        tron_client=client,
    )
    if not quote.submit_ready:
        raise _error_from_quote(quote)

    provisioned = False
    if quote.bandwidth.deficit:
        _provision_bandwidth(quote, client)
        provisioned = True

    if quote.energy.deficit:
        _provision_energy(quote, client)
        provisioned = True

    if not provisioned:
        return quote

    refreshed = quote
    for attempt in range(config.PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS):
        try:
            refreshed = estimate_usdt_transfer_resources(
                source_address,
                destination,
                amount,
                tron_client=client,
            )
        except UsdtResourceError as exc:
            raise UsdtResourceError(
                str(exc),
                code=exc.code,
                temporary=exc.temporary,
                provider_order_accepted=True,
                provider_task_id=exc.provider_task_id,
            ) from exc
        if (
            refreshed.submit_ready
            and refreshed.energy.deficit == 0
            and refreshed.bandwidth.deficit == 0
        ):
            return refreshed
        if attempt + 1 < config.PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_ATTEMPTS:
            time.sleep(config.PAYOUT_RESOURCE_POST_ACTIVE_RECHECK_SLEEP_SEC)

    raise UsdtResourceError(
        refreshed.blocking_reason
        or "TRON USDT transfer resources are still insufficient after provider provisioning",
        code=refreshed.blocking_code or "RESOURCE_RECHECK_FAILED",
        temporary=(
            (refreshed.blocking_code or "RESOURCE_RECHECK_FAILED")
            in TEMPORARY_RESOURCE_BLOCKING_CODES
        ),
        provider_order_accepted=True,
    )


def _quote_from_resources(
    source_address: str,
    destination: str,
    amount: Decimal,
    account_resource: dict,
    estimate_provider: str | None,
    fee_estimate: dict | None,
    estimate_failure,
    *,
    tron_client,
) -> UsdtResourceQuote:
    blocking_code = None
    blocking_reason = None
    activation_required = False
    estimated_trx_burned = None

    if fee_estimate is None:
        energy_required = 0
        if estimate_failure is not None:
            blocking_code = estimate_failure.code
            blocking_reason = (
                "Unable to estimate TRON USDT transfer energy: "
                f"{estimate_failure.code}"
            )
        else:
            blocking_code = "RESOURCE_ESTIMATE_UNAVAILABLE"
            blocking_reason = "Unable to estimate TRON USDT transfer energy"
    else:
        energy_required = fee_estimate["energy_required"]
        activation_required = bool(fee_estimate.get("is_new_address"))
        trx_burned = fee_estimate.get("trx_burned")
        estimated_trx_burned = str(trx_burned) if trx_burned is not None else None
        if activation_required:
            blocking_code = "DESTINATION_NOT_ACTIVATED"
            blocking_reason = "TRON USDT destination is not activated"

    energy_available = get_available_energy(account_resource)
    energy_deficit = max(energy_required - energy_available, 0)
    bandwidth_required = config.BANDWIDTH_PER_TRC20_TRANSFER_CALL
    bandwidth_available = get_available_bandwidth(account_resource)
    bandwidth_deficit = max(bandwidth_required - bandwidth_available, 0)

    energy_providers = _energy_provider_chain(tron_client) if energy_deficit else []
    bandwidth_providers = (
        _bandwidth_provider_chain(tron_client) if bandwidth_deficit else []
    )
    energy_provider_label = energy_providers[0][0] if energy_providers else None
    bandwidth_provider_label = (
        bandwidth_providers[0][0] if bandwidth_providers else None
    )

    if blocking_reason is None and energy_deficit and not energy_providers:
        blocking_code = "PROVIDER_UNAVAILABLE"
        blocking_reason = "No energy provider is configured for TRON USDT resources"
    elif blocking_reason is None and bandwidth_deficit and not bandwidth_providers:
        blocking_code = "PROVIDER_UNAVAILABLE"
        blocking_reason = "No bandwidth provider is configured for TRON USDT resources"

    return UsdtResourceQuote(
        source_address=source_address,
        destination=destination,
        amount=str(amount),
        estimate_provider=estimate_provider,
        activation_required=activation_required,
        estimated_trx_burned=estimated_trx_burned,
        energy=UsdtResourceReadiness(
            provider=energy_provider_label,
            required=energy_required,
            available=energy_available,
            deficit=energy_deficit,
        ),
        bandwidth=UsdtResourceReadiness(
            provider=bandwidth_provider_label,
            required=bandwidth_required,
            available=bandwidth_available,
            deficit=bandwidth_deficit,
        ),
        submit_ready=blocking_reason is None,
        blocking_code=blocking_code,
        blocking_reason=blocking_reason,
    )


def _provision_bandwidth(quote: UsdtResourceQuote, client) -> None:
    providers = _bandwidth_provider_chain(client)
    if not providers:
        raise UsdtResourceError(
            "No bandwidth provider is configured",
            code="PROVIDER_UNAVAILABLE",
        )

    for provider_name, provider in providers:
        logger.info(
            "Preparing TRON USDT bandwidth: provider=%s source=%s required=%s deficit=%s",
            provider_name,
            quote.source_address,
            quote.bandwidth.required,
            quote.bandwidth.deficit,
        )
        if provider.acquire_bandwidth(
            quote.source_address,
            quote.bandwidth.required,
        ):
            return
        if not _provider_fallback_eligible(provider):
            _raise_provider_failure(provider, "Bandwidth")
        if _bandwidth_ready(client, quote.source_address, quote.bandwidth.required):
            return
        logger.warning(
            "TRON USDT bandwidth provider failed; trying fallback if available: "
            "provider=%s source=%s",
            provider_name,
            quote.source_address,
        )

    raise UsdtResourceError(
        "Bandwidth provider failed to prepare resources",
        code="PROVIDER_FAILED",
        temporary=True,
    )


def _provision_energy(quote: UsdtResourceQuote, client) -> None:
    providers = _energy_provider_chain(client)
    if not providers:
        raise UsdtResourceError(
            "No energy provider is configured",
            code="PROVIDER_UNAVAILABLE",
        )

    account_resource = _get_account_resources(client, quote.source_address)
    for provider_name, provider in providers:
        energy_available = get_available_energy(account_resource)
        energy_deficit = max(quote.energy.required - energy_available, 0)
        if energy_deficit == 0:
            return
        logger.info(
            "Preparing TRON USDT energy: provider=%s source=%s required=%s deficit=%s",
            provider_name,
            quote.source_address,
            quote.energy.required,
            energy_deficit,
        )
        if provider.acquire_energy(
            quote.source_address,
            energy_deficit,
            account_resource,
            minimum_energy_required=quote.energy.required,
            strict_minimum_required=True,
        ):
            return
        if not _provider_fallback_eligible(provider):
            _raise_provider_failure(provider, "Energy")
        account_resource = _get_account_resources(client, quote.source_address)
        if get_available_energy(account_resource) >= quote.energy.required:
            return
        logger.warning(
            "TRON USDT energy provider failed; trying fallback if available: "
            "provider=%s source=%s",
            provider_name,
            quote.source_address,
        )

    raise UsdtResourceError(
        "Energy provider failed to prepare resources",
        code="PROVIDER_FAILED",
        temporary=True,
    )


def _error_from_quote(quote: UsdtResourceQuote) -> UsdtResourceError:
    return UsdtResourceError(
        quote.blocking_reason or "TRON USDT transfer resources are not ready",
        code=quote.blocking_code,
        temporary=quote.blocking_code in TEMPORARY_RESOURCE_BLOCKING_CODES,
    )

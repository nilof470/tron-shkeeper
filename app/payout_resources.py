from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

import tronpy.keys
import tronpy.exceptions

from app.config import config
from app.connection_manager import ConnectionManager
from app.logging import logger
from app.payout_destination_activation import (
    DestinationActivationError,
    ensure_destination_activated,
)
from app.resource_providers.profeex import ProfeeXProvider
from app.schemas import KeyType
from app.usdt_resource_provisioning import (
    UsdtResourceError,
    UsdtResourceQuote,
    estimate_usdt_transfer_resources,
    ensure_usdt_transfer_resources,
)
from app.utils import get_key


TEMPORARY_RESOURCE_BLOCKING_CODES = {
    "RESOURCE_ESTIMATE_UNAVAILABLE",
    "RESOURCE_READ_FAILED",
    "RESOURCE_RECHECK_FAILED",
    "PROVIDER_FAILED",
}


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
    estimate_provider: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class PayoutResourceError(RuntimeError):
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


def estimate_usdt_transfer_fee_via_profeex(destination: str) -> dict | None:
    return ProfeeXProvider().estimate_usdt_transfer_fee(destination)


def _destination_is_active(client, destination: str) -> bool | None:
    try:
        client.get_account(destination)
    except tronpy.exceptions.AddressNotFound:
        return False
    except Exception:
        logger.exception(
            "Unable to read TRON payout destination account activation status"
        )
        return None
    return True


def _activation_quote_via_chain_or_profeex(client, destination: str) -> dict | None:
    active = _destination_is_active(client, destination)
    if active is True:
        return {"is_new_address": False}
    if active is False:
        return {"is_new_address": True}
    return None


def _destination_activation_quote_fn(client, quote: PayoutResourceQuote):
    if quote.estimate_provider == "refee":
        return lambda receiver: _activation_quote_via_chain_or_profeex(
            client,
            receiver,
        )
    return lambda receiver: estimate_usdt_transfer_fee_via_profeex(receiver)


def _readiness_from_usdt(readiness) -> ResourceReadiness:
    return ResourceReadiness(
        provider=readiness.provider,
        required=readiness.required,
        available=readiness.available,
        deficit=readiness.deficit,
    )


def _quote_from_usdt(quote: UsdtResourceQuote) -> PayoutResourceQuote:
    return PayoutResourceQuote(
        source_address=quote.source_address,
        destination=quote.destination,
        amount=quote.amount,
        activation_required=quote.activation_required,
        estimated_trx_burned=quote.estimated_trx_burned,
        energy=_readiness_from_usdt(quote.energy),
        bandwidth=_readiness_from_usdt(quote.bandwidth),
        submit_ready=quote.submit_ready,
        blocking_code=quote.blocking_code,
        blocking_reason=quote.blocking_reason,
        estimate_provider=quote.estimate_provider,
    )


def _blocked_quote(
    quote: PayoutResourceQuote,
    *,
    code: str,
    reason: str,
    activation_required: bool | None = None,
) -> PayoutResourceQuote:
    return PayoutResourceQuote(
        source_address=quote.source_address,
        destination=quote.destination,
        amount=quote.amount,
        activation_required=(
            quote.activation_required
            if activation_required is None
            else activation_required
        ),
        estimated_trx_burned=quote.estimated_trx_burned,
        energy=quote.energy,
        bandwidth=quote.bandwidth,
        submit_ready=False,
        blocking_code=code,
        blocking_reason=reason,
        estimate_provider=quote.estimate_provider,
    )


def _quote_with_refee_activation_check(
    client,
    quote: PayoutResourceQuote,
) -> PayoutResourceQuote:
    if quote.estimate_provider != "refee" or quote.activation_required:
        return quote
    active = _destination_is_active(client, quote.destination)
    if active is True:
        return quote
    if active is False:
        return _blocked_quote(
            quote,
            code="DESTINATION_NOT_ACTIVATED",
            reason="TRON payout destination is not activated",
            activation_required=True,
        )
    return _blocked_quote(
        quote,
        code="RESOURCE_READ_FAILED",
        reason="Unable to read TRON payout destination account activation status",
    )


def _payout_error_from_usdt(exc: UsdtResourceError) -> PayoutResourceError:
    return PayoutResourceError(
        str(exc),
        code=exc.code,
        temporary=exc.temporary,
        provider_order_accepted=exc.provider_order_accepted,
        provider_task_id=exc.provider_task_id,
    )


def _raise_from_quote(quote: PayoutResourceQuote) -> None:
    raise PayoutResourceError(
        quote.blocking_reason or "TRON USDT payout resources are not ready",
        code=quote.blocking_code,
        temporary=quote.blocking_code in TEMPORARY_RESOURCE_BLOCKING_CODES,
    )


def estimate_fee_deposit_resources_for_usdt_payout(
    destination: str,
    amount: Decimal,
    *,
    tron_client=None,
) -> PayoutResourceQuote:
    tronpy.keys.to_base58check_address(destination)
    client = tron_client or ConnectionManager.client()
    _, fee_deposit_address = get_key(KeyType.fee_deposit)
    try:
        usdt_quote = estimate_usdt_transfer_resources(
            fee_deposit_address,
            destination,
            amount,
            tron_client=client,
        )
    except UsdtResourceError as exc:
        raise _payout_error_from_usdt(exc) from exc
    quote = _quote_from_usdt(usdt_quote)
    return _quote_with_refee_activation_check(client, quote)


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
    activation_performed = False
    if (
        not quote.submit_ready
        and quote.blocking_code == "DESTINATION_NOT_ACTIVATED"
        and allow_destination_activation
        and config.TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION
    ):
        try:
            ensure_destination_activated(
                destination,
                quote_fn=_destination_activation_quote_fn(client, quote),
            )
        except DestinationActivationError as exc:
            raise PayoutResourceError(
                str(exc), code=exc.code, temporary=exc.temporary
            ) from exc
        activation_performed = True
    if not quote.submit_ready and not activation_performed:
        _raise_from_quote(quote)

    try:
        ensured = ensure_usdt_transfer_resources(
            quote.source_address,
            destination,
            amount,
            tron_client=client,
        )
    except UsdtResourceError as exc:
        raise _payout_error_from_usdt(exc) from exc
    return _quote_from_usdt(ensured)

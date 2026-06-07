from __future__ import annotations

from decimal import Decimal

from .block_scanner import parse_tx
from .celery_readiness import usdt_payout_worker_ready
from .connection_manager import ConnectionManager
from .config import config
from .payout_resources import estimate_fee_deposit_resources_for_usdt_payout
from .schemas import KeyType, TronSymbol
from .utils import get_key
from .wallet import Wallet


class PayoutStatusError(RuntimeError):
    def __init__(self, message, *, code, status_code=400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def source_wallet_address(source_wallet_ref):
    if source_wallet_ref != "fee_deposit":
        raise PayoutStatusError(
            f"Unsupported source wallet reference: {source_wallet_ref}",
            code="PAYOUT_SOURCE_WALLET_MISMATCH",
        )
    _, address = get_key(KeyType.fee_deposit)
    return address


def expected_source_wallet_address(row):
    import json

    metadata = json.loads(row["chain_check_metadata"] or "{}")
    return metadata.get("source_wallet_address") or source_wallet_address(
        row["source_wallet"]
    )


def run_tron_usdt_preflight_checks(canonical, *, allow_destination_auto_activation=False):
    if not usdt_payout_worker_ready():
        raise PayoutStatusError(
            "TRON USDT payout worker is not ready",
            code="PAYOUT_WORKER_UNAVAILABLE",
            status_code=503,
        )

    amount = Decimal(canonical["amount"])
    try:
        wallet = Wallet("USDT")
        balance = wallet.balance
    except Exception as exc:
        raise PayoutStatusError(
            f"Unable to read fee-deposit USDT balance: {exc}",
            code="PAYOUT_BALANCE_UNAVAILABLE",
            status_code=503,
        ) from exc
    if balance < amount:
        raise PayoutStatusError(
            "Fee-deposit wallet does not have enough USDT for payout",
            code="INSUFFICIENT_USDT",
            status_code=409,
        )

    try:
        quote = estimate_fee_deposit_resources_for_usdt_payout(
            canonical["destination"],
            amount,
        )
    except Exception as exc:
        raise PayoutStatusError(
            f"Unable to verify TRON USDT payout resources: {exc}",
            code=getattr(exc, "code", None) or "PAYOUT_RESOURCE_UNAVAILABLE",
            status_code=503,
        ) from exc
    if not quote.submit_ready:
        if (
            allow_destination_auto_activation
            and config.TRON_USDT_PAYOUT_AUTO_ACTIVATE_DESTINATION
            and quote.blocking_code == "DESTINATION_NOT_ACTIVATED"
        ):
            return {
                "resource_quote": quote.to_dict(),
                "destination_activation_submit_eligible": True,
            }
        raise PayoutStatusError(
            quote.blocking_reason or "TRON USDT payout resources are not ready",
            code=quote.blocking_code or "PAYOUT_RESOURCE_UNAVAILABLE",
            status_code=503,
        )
    return {"resource_quote": quote.to_dict()}


def _confirmation_progress(client, tx_info):
    block_number = tx_info.get("blockNumber") or tx_info.get("block_number")
    progress = {
        "confirmations": 0,
        "min_confirmations": config.TRON_USDT_PAYOUT_MIN_CONFIRMATIONS,
        "tx_block_number": block_number,
        "latest_block_number": None,
    }
    if block_number is None:
        progress["confirmation_error"] = "transaction block number is unavailable"
        return progress
    try:
        latest_block = client.get_latest_block_number()
    except Exception as exc:
        progress["confirmation_error"] = str(exc)
        return progress
    progress["latest_block_number"] = latest_block
    progress["confirmations"] = max(int(latest_block) - int(block_number) + 1, 0)
    return progress


def _has_min_confirmations(progress):
    return progress["confirmations"] >= progress["min_confirmations"]


def _metadata(
    row,
    tx,
    tx_info,
    *,
    transfer_match,
    matched_transfer=None,
    error=None,
    confirmation=None,
):
    metadata = {
        "confirmation_check": "TRON_USDT_TRC20_TRANSFER",
        "txid": row["txid"],
        "transaction_found": bool(tx),
        "transaction_info_found": bool(tx_info),
        "transfer_match": transfer_match,
        "expected_source": expected_source_wallet_address(row),
        "expected_destination": _canonical_payload(row)["destination"],
        "expected_amount": _canonical_payload(row)["amount"],
        "expected_token_contract": row["token_contract"],
        "network": row["chain_id_or_network_id"],
    }
    if matched_transfer is not None:
        metadata["matched_transfer"] = {
            "txid": matched_transfer.txid,
            "symbol": str(matched_transfer.symbol),
            "source": matched_transfer.src_addr,
            "destination": matched_transfer.dst_addr,
            "amount": str(matched_transfer.amount),
        }
    if error:
        metadata["error"] = str(error)
    if confirmation is not None:
        metadata.update(confirmation)
    return metadata


def _canonical_payload(row):
    import json

    return json.loads(row["canonical_payload_json"])


def refresh_tron_usdt_confirmation(row, tron_client=None):
    client = tron_client or ConnectionManager.client()
    tx = client.get_transaction(row["txid"])
    tx_info = client.get_transaction_info(row["txid"])
    confirmation = _confirmation_progress(client, tx_info)
    payload = _canonical_payload(row)
    expected_source = expected_source_wallet_address(row)
    expected_amount = Decimal(payload["amount"])
    try:
        transfers = parse_tx(tx, tx_info)
    except Exception as exc:
        if tx.get("ret", [{}])[0].get("contractRet") not in (None, "SUCCESS"):
            return {
                "state": "FAILED_CHAIN_TERMINAL",
                "metadata": _metadata(
                    row,
                    tx,
                    tx_info,
                    transfer_match=False,
                    error=exc,
                    confirmation=confirmation,
                ),
                "failure_class": "CHAIN_TERMINAL",
                "error_code": "TRON_TRANSACTION_FAILED",
                "error_message": str(exc),
            }
        return {
            "state": "CONFIRMING",
            "metadata": _metadata(
                row,
                tx,
                tx_info,
                transfer_match=False,
                error=exc,
                confirmation=confirmation,
            ),
        }

    for transfer in transfers:
        if (
            transfer.is_trc20
            and transfer.symbol == TronSymbol.USDT
            and transfer.src_addr == expected_source
            and transfer.dst_addr == payload["destination"]
            and transfer.amount == expected_amount
        ):
            metadata = _metadata(
                row,
                tx,
                tx_info,
                transfer_match=True,
                matched_transfer=transfer,
                confirmation=confirmation,
            )
            if not _has_min_confirmations(confirmation):
                return {
                    "state": "CONFIRMING",
                    "metadata": metadata,
                }
            return {
                "state": "CONFIRMED",
                "metadata": metadata,
            }

    metadata = _metadata(
        row,
        tx,
        tx_info,
        transfer_match=False,
        confirmation=confirmation,
    )
    if _has_min_confirmations(confirmation):
        return {
            "state": "FAILED_CHAIN_TERMINAL",
            "metadata": metadata,
            "failure_class": "CHAIN_TERMINAL",
            "error_code": "TRON_USDT_TRANSFER_NOT_FOUND",
            "error_message": (
                "Confirmed TRON transaction does not contain the expected "
                "USDT transfer"
            ),
        }
    return {
        "state": "CONFIRMING",
        "metadata": metadata,
    }

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

import tronpy.exceptions
from tronpy.keys import PrivateKey

from .config import config
from .db import query_db2
from .fee_deposit_spend_guard import fee_deposit_spend_guard_for_address
from .logging import logger
from .connection_manager import ConnectionManager
from .wallet_encryption import wallet_encryption
from .schemas import TronAddress


class Wallet:
    CACHE = {
        "decimals": {},
        "contracts": {},
    }
    main_account = query_db2('select * from keys where type = "fee_deposit" ', one=True)

    def __init__(self, symbol="TRX"):
        self.symbol = symbol
        self.client = ConnectionManager.client()
        if symbol != "TRX":
            self.contract_address = config.get_contract_address(symbol)

    def get_contract(self, contract_address=None):
        if contract_address is None:
            contract_address = self.contract_address
        contract = self.CACHE["contracts"].get(contract_address)
        if not contract:
            contract = self.client.get_contract(contract_address)
            self.CACHE["contracts"][contract_address] = contract
        decimals = self.CACHE["decimals"].get(contract_address)
        if not decimals:
            self.CACHE["decimals"][contract_address] = contract.functions.decimals()
        return contract

    @property
    def balance(self):
        return self.balance_of(self.main_account["public"])

    def balance_of(self, address):
        if self.symbol == "TRX":
            try:
                return self.client.get_account_balance(address)
            except tronpy.exceptions.AddressNotFound:
                return Decimal(0)
        else:
            return (
                Decimal(self.get_contract().functions.balanceOf(address))
                / 10 ** self.CACHE["decimals"][self.contract_address]
            )

    def bandwidth_of(self, address):
        res = self.client.get_account_resource(address)
        logger.debug(f"Resources of {address}: {res}")
        bandwidth = res.get("freeNetLimit", 0) - res.get("freeNetUsed", 0)
        return Decimal(bandwidth)

    def _source_account(self, src_address: TronAddress = None):
        if src_address:
            return query_db2(
                "select * from keys where public = ?", (src_address,), one=True
            )
        return self.main_account

    def build_signed_transfer(
        self,
        dst,
        amount,
        src_address: TronAddress = None,
        *,
        expiration_ms: int | None = None,
    ):
        src_account = self._source_account(src_address)
        if self.symbol == "TRX":
            txn = self.client.trx.transfer(
                src_account["public"], dst, int(amount * 1_000_000)
            )
        else:
            txn = (
                self.get_contract()
                .functions.transfer(
                    dst, int(amount * (10 ** config.get_decimal(self.symbol)))
                )
                .with_owner(src_account["public"])
                .fee_limit(int(config.TX_FEE_LIMIT * 1_000_000))
            )

        if expiration_ms is None:
            # https://github.com/tronprotocol/java-tron/issues/2883#issuecomment-575007235
            txn._raw_data["expiration"] += 12 * 60 * 60 * 1_000  # 12 hours
        else:
            txn._raw_data["expiration"] = txn._raw_data["timestamp"] + int(expiration_ms)
        return txn.build().sign(
            PrivateKey(bytes.fromhex(wallet_encryption.decrypt(src_account["private"])))
        )

    @staticmethod
    def signed_transfer_evidence(txn):
        try:
            payload = txn.to_json()
        except AttributeError:
            payload = repr(txn)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        signed_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        raw_data = payload.get("raw_data", {}) if isinstance(payload, dict) else {}
        ref_block_bytes = raw_data.get("ref_block_bytes")
        ref_block_hash = raw_data.get("ref_block_hash")
        reference_block = (
            f"{ref_block_bytes}:{ref_block_hash}"
            if ref_block_bytes and ref_block_hash
            else None
        )
        expiration_at = None
        expiration = raw_data.get("expiration")
        if expiration is not None:
            expiration_at = (
                datetime.fromtimestamp(int(expiration) / 1000, tz=timezone.utc)
                .replace(tzinfo=None)
                .isoformat(timespec="microseconds")
                + "Z"
            )
        return {
            "signed_raw_tx_ref": f"not-retained:signed-tron-tx-sha256:{signed_hash}",
            "signed_raw_tx_hash": signed_hash,
            "txid": txn.txid,
            "reference_block": reference_block,
            "expiration_at": expiration_at,
            "chain_check_metadata": {
                "signed_tx_artifact_retention": "NOT_RETAINED_SPENDABLE_RAW_TX",
                "signed_tx_hash_algorithm": "sha256",
                "signed_tx_json_hash": signed_hash,
                "signed_tx_txid": txn.txid,
            },
        }

    @staticmethod
    def broadcast_signed_transfer(txn):
        result = txn.broadcast()
        tx_info = result.wait()
        if isinstance(tx_info, dict):
            tx_info.setdefault("txid", getattr(result, "txid", None))
            tx_info.setdefault("broadcast_txid", getattr(result, "txid", None))
        return tx_info

    def transfer_result(self, dst, amount, txn, txn_res):
        logger.info(
            f"{amount} {self.symbol} has been sent to {dst} with TXID {txn.txid}. Details: {txn_res}"
        )

        result = {
            "dest": dst,
            "amount": str(amount),
            "txids": [txn.txid],
            "details": txn_res,
        }

        if self.symbol == "TRX":
            if txn_res["contractResult"] == [""]:
                result["status"] = "success"
            else:
                result["status"] = "error"
                result["message"] = f"contractResult: {txn_res['contractResult']}"
        else:
            if txn_res["receipt"]["result"] == "SUCCESS":
                result["status"] = "success"
            else:
                result["status"] = "error"
                result["message"] = f"{txn_res['result']}: {txn_res['resMessage']}"

        return result

    def transfer(self, dst, amount, src_address: TronAddress = None):
        src_account = self._source_account(src_address)
        with fee_deposit_spend_guard_for_address(
            src_account["public"],
            reason=f"wallet-transfer:{self.symbol}",
        ):
            txn = self.build_signed_transfer(dst, amount, src_address=src_address)
            txn_res = self.broadcast_signed_transfer(txn)
        return self.transfer_result(dst, amount, txn, txn_res)

import collections
import concurrent
from contextlib import contextmanager
from contextlib import closing
import datetime
import decimal
from functools import cache, lru_cache
import sqlite3
import time
import uuid
from decimal import Decimal
from typing import Dict, List

from celery import Celery
from celery.schedules import crontab
from pydantic import TypeAdapter
from tronpy.keys import PrivateKey
from tronpy.tron import current_timestamp
from tronpy.abi import trx_abi
import tronpy.exceptions
from sqlmodel import Session, select

from app.schemas import KeyType

from . import celery
from .config import config
from .db import query_db, query_db2
from .fee_deposit_spend_guard import (
    fee_deposit_spend_guard_for_address,
    fee_deposit_spend_lock,
)
from .wallet import Wallet
from .utils import (
    est_vote_tx_bw_cons,
    get_available_energy,
    get_energy_delegator,
    get_key,
    has_free_bw,
    skip_if_running,
)
from .connection_manager import ConnectionManager
from .payout_callback_outbox import (
    claim_due_payout_callbacks,
    create_payout_callback,
    dispatch_payout_callback,
    should_retry,
)
from .payout_resources import ensure_fee_deposit_resources_for_usdt_payout
from .resource_providers import get_bandwidth_provider, get_energy_provider
from .logging import logger
from .wallet_encryption import wallet_encryption
from .sweep_guard import is_sweep_allowed


@contextmanager
def usdt_payout_resource_lock():
    with fee_deposit_spend_lock(reason="tron-usdt-payout"):
        yield


def estimate_trc20_sweep_energy(
    symbol,
    provider,
    receiver_address,
    onetime_address,
    contract_address,
    tron_client,
):
    if symbol == "USDT" and config.ENERGY_PROVIDER == "profeex":
        logger.info(
            "Estimate the amount of energy needed to make USDT transfer via ProfeeX"
        )
        estimate = provider.estimate_usdt_transfer_fee(receiver_address)
        if estimate is None:
            logger.warning(
                "Unable to estimate USDT transfer energy through ProfeeX. "
                "Terminating transfer."
            )
            return None
        energy_required = estimate["energy_required"]
        logger.info(
            "ProfeeX estimated amount of energy for USDT transfer is: "
            f"{energy_required}. Details: {estimate}"
        )
        return energy_required

    if symbol == "USDT" and config.ENERGY_PROVIDER == "refee":
        energy_required = int(config.REFEE_FIXED_ENERGY_ORDER_AMOUNT)
        if energy_required <= 0:
            logger.warning(
                "REFEE_FIXED_ENERGY_ORDER_AMOUNT must be greater than 0 for "
                "USDT sweep energy provisioning. Terminating transfer."
            )
            return None
        logger.info(
            "Using fixed re:Fee amount as USDT sweep energy requirement: "
            f"{energy_required}"
        )
        return energy_required

    logger.info("Estimate the amount of energy needed to make transfer")
    energy_required = tron_client.get_estimated_energy(
        onetime_address,
        contract_address,
        "transfer(address,uint256)",
        trx_abi.encode_single("(address,uint256)", (receiver_address, 42)).hex(),
    )
    logger.info(f"Estimated amount of energy for transfer is: {energy_required}")
    return energy_required


@celery.task()
def prepare_payout(dest, amount, symbol):
    if (balance := Wallet(symbol).balance) < amount:
        raise Exception(
            f"Wallet balance is less than payout amount: {balance} < {amount}"
        )
    steps = []
    steps.append(
        {
            "dst": dest,
            "amount": decimal.Decimal(amount),
            "ensure_usdt_payout_resources": (
                config.TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED
                and symbol == "USDT"
            ),
        }
    )
    return steps


@celery.task()
def prepare_multipayout(payout_list, symbol):
    logger.info(
        f"Preparing payout for {sum([t['amount'] for t in payout_list])} "
        f"{symbol} to {len(payout_list)} destinations."
    )
    steps = []
    for payout in payout_list:
        steps.append(
            {
                "dst": payout["dest"],
                "amount": decimal.Decimal(payout["amount"]),
                "ensure_usdt_payout_resources": (
                    config.TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED
                    and symbol == "USDT"
                ),
            }
        )
    return steps


@celery.task()
def payout(steps, symbol):
    wallet = Wallet(symbol)
    payout_results = []
    try:
        if any(step.get("ensure_usdt_payout_resources") for step in steps):
            for step in steps:
                if step.get("ensure_usdt_payout_resources"):
                    with usdt_payout_resource_lock():
                        ensure_fee_deposit_resources_for_usdt_payout(
                            step["dst"],
                            step["amount"],
                            tron_client=wallet.client,
                        )
                        result = wallet.transfer(step["dst"], step["amount"])
                    payout_results.append(result)
                    if result.get("status") != "success":
                        raise Exception(f"USDT payout transfer failed: {result}")
                else:
                    payout_results.append(wallet.transfer(step["dst"], step["amount"]))
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=config.CONCURRENT_MAX_WORKERS
            ) as executor:
                for result in executor.map(
                    lambda x: wallet.transfer(x["dst"], x["amount"]),
                    steps,
                ):
                    payout_results.append(result)
    except Exception:
        completed_results = [
            result
            for result in payout_results
            if result.get("status") == "success" or result.get("txids")
        ]
        if completed_results:
            queue_payout_callback(completed_results, symbol)
        raise
    queue_payout_callback(payout_results, symbol)
    return payout_results


@celery.task(bind=True)
def execute_payout_execution(self, execution_id):
    from .payout_execution import PayoutExecutionStore

    wallet = Wallet("USDT")
    return PayoutExecutionStore.execute(
        execution_id,
        wallet=wallet,
        resource_ensurer=ensure_fee_deposit_resources_for_usdt_payout,
        lock_factory=usdt_payout_resource_lock,
        lease_owner=self.request.id,
    )


def _fund_onetime_for_trc20_burn(
    tron_client,
    main_publ_key,
    main_priv_key,
    onetime_publ_key,
    balance,
    symbol,
    min_threshold,
):
    logger.info("Transferring TRC20 tokens from onetime to main in TRX burning mode")
    logger.info(
        f"Transfer to main acc started for {onetime_publ_key}. Balance: "
        f"{balance} {symbol}. Threshold is {min_threshold} {symbol}"
    )

    with fee_deposit_spend_guard_for_address(
        main_publ_key,
        reason="trc20-sweep-fee-funding",
    ):
        main_acc_balance = tron_client.get_account_balance(main_publ_key)

        if main_acc_balance < config.get_internal_trc20_tx_fee():
            logger.warning(
                f"Main account hasn't enough currency: balance: {main_acc_balance} need: {config.get_internal_trc20_tx_fee()}.  Terminating transfer."
            )
            return False, None

        tx_trx = tron_client.trx.transfer(
            main_publ_key,
            onetime_publ_key,
            int(config.get_internal_trc20_tx_fee() * 1_000_000),
        )
        tx_trx._raw_data["expiration"] = current_timestamp() + 60_000
        tx_trx = tx_trx.build()
        tx_trx = tx_trx.sign(main_priv_key)
        tx_trx_res = tx_trx.broadcast().wait()
    logger.info(
        f"Fee sent to {onetime_publ_key} with TXID {tx_trx.txid}. Details: {tx_trx_res}"
    )
    return True, tx_trx_res


def _trc20_transfer_succeeded(tx_info: dict) -> bool:
    return tx_info.get("receipt", {}).get("result") == "SUCCESS"


def ensure_onetime_bandwidth(onetime_publ_key: str, tron_client) -> bool:
    required_bandwidth = config.BANDWIDTH_PER_TRC20_TRANSFER_CALL
    logger.info("Check onetime account bandwidth before energy provisioning")
    if has_free_bw(onetime_publ_key, required_bandwidth, tron_client=tron_client):
        logger.info("Onetime account has enough bandwidth")
        return True

    if config.BANDWIDTH_PROVIDER == "disabled":
        logger.warning(
            "One-time account has no bandwidth and external bandwidth rental is "
            "disabled. Leaving sweep for a later retry after TRON bandwidth recovery."
        )
        return False

    bandwidth_provider = get_bandwidth_provider(tron_client=tron_client)
    if bandwidth_provider is None:
        logger.warning(
            "One-time account has no bandwidth and no bandwidth provider is configured."
        )
        return False

    logger.info(
        "One-time account has no bandwidth. "
        f"Requesting {config.BANDWIDTH_PROVIDER} bandwidth before energy provisioning."
    )
    if not bandwidth_provider.acquire_bandwidth(onetime_publ_key, required_bandwidth):
        logger.warning(
            "One-time account has no bandwidth after provider rental. "
            "Terminating transfer before energy provisioning."
        )
        return False

    return True


@celery.task()
def transfer_trc20_from(onetime_acc, symbol, txid=None):
    """
    Transfers TRC20 from onetime to main account
    """

    if not is_sweep_allowed(symbol, onetime_acc, txid=txid):
        logger.info(
            "SHKeeper sweep eligibility did not allow guarded sweep before "
            f"touching chain state: symbol={symbol} account={onetime_acc} txid={txid}"
        )
        return False

    tron_client = ConnectionManager.client()

    contract_address = config.get_contract_address(symbol)
    contract = tron_client.get_contract(contract_address)
    precision = contract.functions.decimals()

    main_priv_key, main_publ_key = get_key(KeyType.fee_deposit)

    if onetime_acc == main_publ_key:
        logger.warning(
            "Transfer from main account is not allowed. Terminating transfer."
        )
        return False

    onetime_priv_key, onetime_publ_key = get_key(KeyType.onetime, pub=onetime_acc)

    token_balance = contract.functions.balanceOf(onetime_publ_key)

    tx_trx_res = None
    used_trx_burn_fallback = False
    use_refee_energy_provider = config.ENERGY_PROVIDER == "refee"
    use_external_energy_provider = config.ENERGY_PROVIDER in {"refee", "profeex"}
    use_staking_energy_provider = (
        config.ENERGY_PROVIDER == "staking" and config.ENERGY_DELEGATION_MODE
    )
    use_energy_provider = use_external_energy_provider or use_staking_energy_provider

    logger.info(f"Check ONETIME={onetime_publ_key} {symbol} balance")
    min_threshold = config.get_min_transfer_threshold(symbol)
    balance = Decimal(token_balance) / 10**precision
    if balance <= min_threshold:
        logger.warning(
            f"Treshold not reached for {onetime_publ_key}. Has: {balance} {symbol} need: {min_threshold} {symbol}. Terminating transfer."
        )
        return
    else:
        logger.info(
            f"Balance OK: {balance} {symbol}. Threshold: {min_threshold} {symbol}"
        )

    if use_energy_provider:
        # Bind once for both acquire (delegation) and release (post-transfer undelegate) calls.
        provider = get_energy_provider(tron_client=tron_client)
        logger.info(f"Using energy provider: {config.ENERGY_PROVIDER}")

        logger.info(
            f"Initiating TRC20 tokens transfer from ONETIME={onetime_publ_key} to MAIN={main_publ_key} in ENERGY DELEGATION MODE"
        )

        if use_staking_energy_provider:
            _, energy_delegator_pub = get_energy_delegator()
            need_bw = (
                config.BANDWIDTH_PER_DELEGE_CALL
                + config.BANDWIDTH_PER_UNDELEGATE_CALL
                + config.BANDWIDTH_PER_TRX_TRANSFER
            )
            logger.info(f"Estimated bandwidth requirement: {need_bw}")

            logger.info("Check energy delegator bandwidth")
            if has_free_bw(energy_delegator_pub, need_bw, tron_client=tron_client):
                logger.info("Using free bandwidth")
            else:
                logger.info("Not enough free bandwidth")
                if config.ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH:
                    logger.info("Burning TRX for bandwidth")
                else:
                    logger.warning(
                        "Burning TRX for bandwidth is not allowed. Terminating transfer."
                    )
                    return

        try:
            onetime_address_resources = tron_client.get_account_resource(
                onetime_publ_key
            )
            logger.info(
                f"Onetime {onetime_publ_key} is already on chain, skipping activation. Resource details {onetime_address_resources=}"
            )
        except tronpy.exceptions.AddressNotFound:
            TRX_FOR_ACTIVATION = "1.1"
            logger.info(
                f"Check if main account has {TRX_FOR_ACTIVATION} TRX for activation"
            )
            main_trx_balance = tron_client.get_account_balance(main_publ_key)
            logger.info(f"Main account balance: {main_trx_balance} TRX")
            if main_trx_balance < Decimal(TRX_FOR_ACTIVATION):
                logger.warning(
                    f"Not enough TRX to activate {onetime_publ_key}. Terminating transfer."
                )
                return
            else:
                logger.info("Main account TRX balance OK.")

            logger.info("Check main account free bandwidth")
            if has_free_bw(
                main_publ_key,
                config.BANDWIDTH_PER_TRX_TRANSFER,
                use_only_staked=True,
                tron_client=tron_client,
            ):
                logger.info("Using main account free bandwidth")
            else:
                logger.info("Main account has not enough free bandwidth")
                if config.ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_FOR_BANDWITH:
                    logger.info("Burning TRX for bandwidth")
                else:
                    logger.warning(
                        "Burning TRX for bandwidth is not allowed. Terminating transfer."
                    )
                    return

            with fee_deposit_spend_guard_for_address(
                main_publ_key,
                reason="trc20-sweep-account-activation",
            ):
                logger.info(f"Activating {onetime_publ_key} by sending 0.1 TRX")
                tx_trx = tron_client.trx.transfer(
                    main_publ_key,
                    onetime_publ_key,
                    int(0.1 * 1_000_000),
                )
                tx_trx._raw_data["expiration"] = current_timestamp() + 60_000
                tx_trx = tx_trx.build()
                tx_trx = tx_trx.sign(main_priv_key)
                tx_trx_res = tx_trx.broadcast().wait()
            logger.info(f"0.1 TRX sent. Details: {tx_trx_res}")
            onetime_address_resources = tron_client.get_account_resource(
                onetime_publ_key
            )
            try:
                onetime_address_resources = tron_client.get_account_resource(
                    onetime_publ_key
                )
            except tronpy.exceptions.AddressNotFound:
                logger.warning(
                    "Onetime acount still not on chain after activation. Terminating transfer."
                )
                return

        if not ensure_onetime_bandwidth(onetime_publ_key, tron_client):
            return

        energy_needed = estimate_trc20_sweep_energy(
            symbol,
            provider,
            main_publ_key,
            onetime_publ_key,
            contract_address,
            tron_client,
        )
        if energy_needed is None:
            return

        logger.info("Check the energy of onetime address")

        onetime_energy_available = get_available_energy(onetime_address_resources)
        if onetime_energy_available >= energy_needed:
            logger.info(
                f"Onetime account {onetime_publ_key} has {onetime_energy_available} "
                f"of {energy_needed} energy. Skipping delegation."
            )

        else:
            logger.info(
                f"Onetime account {onetime_publ_key} has {onetime_energy_available} "
                f"of {energy_needed} energy"
            )

            if use_external_energy_provider:
                energy_to_provision = energy_needed - onetime_energy_available
            else:
                logger.info("Check if energy was alread delegated")

                onetime_delegated_resources = (
                    tron_client.get_delegated_resource_account_index_v2(
                        onetime_publ_key
                    )
                )

                if "fromAccounts" in onetime_delegated_resources:
                    logger.info(
                        f"Found delegated energy on onetime account. Details {onetime_delegated_resources=}"
                    )

                    if onetime_energy_available < energy_needed:
                        logger.warning(
                            "Onetime account has not enough energy after previous delegation."
                        )

                        if (
                            config.ENERGY_DELEGATION_MODE_ALLOW_ADDITIONAL_ENERGY_DELEGATION
                        ):
                            logger.info(
                                "Additional energy delegation is allowed. Calculating the difference."
                            )
                            energy_diff = energy_needed - onetime_energy_available

                            if energy_diff <= 0:
                                logger.warning(
                                    f"Energy diff = {energy_diff}. Terminating transfer."
                                )

                            energy_to_provision = energy_diff
                        else:
                            logger.warning("Terminating transfer.")
                            return
                    else:
                        energy_to_provision = 0
                else:
                    logger.info("No delagated energy found")
                    energy_to_provision = energy_needed - onetime_energy_available

            if energy_to_provision > 0:
                logger.info(
                    f"Requesting energy provider to provision {energy_to_provision} energy on {onetime_publ_key}"
                )
                if not provider.acquire_energy(
                    onetime_publ_key,
                    energy_to_provision,
                    onetime_address_resources,
                    minimum_energy_required=energy_needed,
                ):
                    if (
                        use_refee_energy_provider
                        and config.ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT
                    ):
                        logger.warning(
                            "Energy provider acquire failed; falling back to TRX burn flow."
                        )
                        burn_ready, tx_trx_res = _fund_onetime_for_trc20_burn(
                            tron_client,
                            main_publ_key,
                            main_priv_key,
                            onetime_publ_key,
                            balance,
                            symbol,
                            min_threshold,
                        )
                        if not burn_ready:
                            return
                        used_trx_burn_fallback = True
                    else:
                        return

            # Check available bandwidth before transfer trc20 tokens
            # from one_time to fee_deposit account
            if not used_trx_burn_fallback:
                if not has_free_bw(
                    onetime_publ_key,
                    config.BANDWIDTH_PER_TRC20_TRANSFER_CALL,
                    tron_client=tron_client,
                ):
                    logger.warning(
                        "One-time account has no bandwidth. Terminating transfer."
                    )
                    return
    else:
        burn_ready, tx_trx_res = _fund_onetime_for_trc20_burn(
            tron_client,
            main_publ_key,
            main_priv_key,
            onetime_publ_key,
            balance,
            symbol,
            min_threshold,
        )
        if not burn_ready:
            return

    #
    # Same flow for both modes
    #

    tx_token = contract.functions.transfer(main_publ_key, int(token_balance))
    tx_token = tx_token.with_owner(onetime_publ_key)
    tx_token = tx_token.fee_limit(int(config.TX_FEE_LIMIT * 1_000_000))
    tx_token._raw_data["expiration"] = current_timestamp() + 60_000
    tx_token = tx_token.build()
    tx_token = tx_token.sign(onetime_priv_key)
    tx_token_res = tx_token.broadcast().wait()
    if not _trc20_transfer_succeeded(tx_token_res):
        logger.warning(
            f"{symbol} transfer from {onetime_publ_key} to {main_publ_key} "
            f"failed with {tx_token.txid}. Details: {tx_token_res}"
        )
        return
    logger.info(
        f"{token_balance / 10**precision} {symbol} sent to {main_publ_key} with {tx_token.txid}. Details: {tx_token_res}"
    )

    if use_energy_provider:
        provider.release_energy(onetime_publ_key)

    return {"tx_trx_res": tx_trx_res, "tx_token": tx_token_res}


@celery.task()
def undelegate_energy(receiver):
    logger.info(f"Undelegating energy from onetime account {receiver}")

    tron_client = ConnectionManager.client()

    energy_delegator_priv, energy_delegator_pub = get_energy_delegator()

    result = tron_client.get_delegated_resource_v2(
        fromAddr=energy_delegator_pub, toAddr=receiver
    )
    if "delegatedResource" not in result:
        logger.info(
            f"Onetime account {receiver} has no any resources delegated. Skipping undelegation."
        )
        return
    frozen_balance_for_energy = 0
    for resource in result["delegatedResource"]:
        if (
            "frozen_balance_for_energy" in resource
            and resource["from"] == energy_delegator_pub
        ):
            frozen_balance_for_energy += resource["frozen_balance_for_energy"]
    if not frozen_balance_for_energy:
        logger.info(
            f"Onetime account {receiver} has no energy delegated. "
            f"Skipping undelegation. Resource details: {result}"
        )
        return

    logger.info(
        f"Undelegating {frozen_balance_for_energy / 1_000_000} TRX from {receiver}"
    )

    with fee_deposit_spend_guard_for_address(
        energy_delegator_pub,
        reason="energy-undelegate",
    ):
        unsigned_tx = tron_client.trx.undelegate_resource(
            owner=energy_delegator_pub,
            receiver=receiver,
            balance=frozen_balance_for_energy,
            resource="ENERGY",
        ).build()
        signed_tx = unsigned_tx.sign(energy_delegator_priv)
        undelegate_tx_info = signed_tx.broadcast().wait()

    logger.info(
        f"Undelegated {frozen_balance_for_energy / 1_000_000} TRX from {receiver} with TXID: {unsigned_tx.txid}"
    )
    logger.debug(undelegate_tx_info)


def _should_sweep_trx_balance(balance: Decimal) -> bool:
    return balance >= config.TRX_MIN_TRANSFER_THRESHOLD


@celery.task()
def transfer_trx_from(onetime_publ_key):
    """
    Transfers TRX from onetime to main account
    """
    logger.info(f"Starting TRX transfer from onetime account {onetime_publ_key}")
    main_publ_key = query_db2(
        'select * from keys where type = "fee_deposit" ', one=True
    )["public"]

    if main_publ_key == onetime_publ_key:
        logger.warning("Skipping TRX transfer from main account.")
        return {"status": "error", "error": "Skipping TRX transfer from main account."}

    bw = Wallet().bandwidth_of(onetime_publ_key)
    if bw < config.BANDWIDTH_PER_TRX_TRANSFER:
        logger.info(
            f"{onetime_publ_key} has not enough bandwidth "
            f"for a free transfer ({bw}/{config.BANDWIDTH_PER_TRX_TRANSFER})"
        )
        return

    tron_client = ConnectionManager.client()
    onetime_acc_balance = tron_client.get_account_balance(onetime_publ_key)
    if onetime_acc_balance == 0:
        return {"status": "error", "error": "skipping 0 TRX account"}
    if not _should_sweep_trx_balance(onetime_acc_balance):
        logger.info(
            f"{onetime_publ_key} TRX balance {onetime_acc_balance} is below "
            f"sweep threshold {config.TRX_MIN_TRANSFER_THRESHOLD}. Leaving TRX on account."
        )
        return

    onetime_priv_key = PrivateKey(
        bytes.fromhex(
            wallet_encryption.decrypt(
                query_db2(
                    'select * from keys where type = "onetime" and public = ?',
                    (onetime_publ_key,),
                    one=True,
                )["private"]
            )
        )
    )

    tx_trx = tron_client.trx.transfer(
        onetime_publ_key, main_publ_key, int(onetime_acc_balance * 1_000_000)
    )
    tx_trx._raw_data["expiration"] = current_timestamp() + 60_000
    tx_trx = tx_trx.build()
    tx_trx = tx_trx.sign(onetime_priv_key)
    tx_trx_res = tx_trx.broadcast().wait()
    logger.info(
        f"{onetime_acc_balance} TRX sent to main account ({main_publ_key}) with TXID {tx_trx.txid}. Details: {tx_trx_res}"
    )
    return {"tx_trx_res": tx_trx_res}


def queue_payout_callback(data, symbol):
    try:
        outbox_id = create_payout_callback(data, symbol)
    except Exception as exc:
        logger.exception(
            "Shkeeper payout notification outbox write failed after payout "
            f"completed: symbol={symbol} error={exc}"
        )
        return None
    try:
        post_payout_results.delay(outbox_id)
    except Exception as exc:
        logger.warning(
            "Shkeeper payout notification task enqueue failed; outbox row "
            f"remains pending: outbox_id={outbox_id} error={exc}"
        )
    return outbox_id


@celery.task(bind=True)
def post_payout_results(self, outbox_id):
    result = dispatch_payout_callback(outbox_id, claim_token=self.request.id)
    if should_retry(result):
        logger.warning(
            "Shkeeper payout notification failed; outbox retry remains pending: "
            f"outbox_id={outbox_id} attempts={result['attempts']} "
            f"error={result['last_error']} next_attempt_at={result['next_attempt_at']}"
        )
    elif result and result.get("status") == "FAILED":
        logger.warning(
            "Shkeeper payout notification permanently failed: "
            f"outbox_id={outbox_id} attempts={result.get('attempts')} "
            f"error={result.get('last_error')}"
        )
    return result


@celery.task(bind=True)
def dispatch_due_payout_callbacks(self, limit=None):
    claim_token = self.request.id or f"payout-callback-sweep-{uuid.uuid4()}"
    rows = claim_due_payout_callbacks(
        limit or config.PAYOUT_CALLBACK_SWEEP_LIMIT,
        claim_token=claim_token,
    )
    results = []
    for row in rows:
        results.append(
            dispatch_payout_callback(row["id"], claim_token=claim_token)
        )
    return results


def is_task_running(task_instance, name: str, args: List = None, kwargs: Dict = None):
    workers = task_instance.app.control.inspect().active()
    for worker, tasks in workers.items():
        for task in tasks:
            # check if task name matches
            if task["name"] != name:
                continue
            # check if args is subset of task args
            if args and not (set(args) <= set(task["args"])):
                continue
            # check if kwargs is subset of task kwargs
            if kwargs and not (kwargs.items() <= task["kwargs"].items()):
                continue
            return True
    return False


@celery.task(bind=True)
@skip_if_running
def scan_accounts(self, *args, **kwargs):
    """
    Scans onetime accounts balances (trc20, trx),
    saves it to database and transfers to main account.
    """

    from .db import engine
    from .models import Balance

    with Session(engine) as session:
        stats = {
            "balances": collections.defaultdict(Decimal),
            "exception_num": 0,
        }

        accounts = [
            row["public"]
            for row in query_db('SELECT public FROM keys WHERE type = "onetime"')
        ]

        balances_to_collect = {"trx": [], "trc20": []}

        for index, account in enumerate(accounts, start=1):
            try:
                #
                # TRC20
                #

                for symbol in [token.symbol for token in config.get_tokens()]:
                    contract = ConnectionManager.client().get_contract(
                        config.get_contract_address(symbol)
                    )

                    while ret := 0 < config.CONCURRENT_MAX_RETRIES:
                        try:
                            trc20_balance = Decimal(
                                contract.functions.balanceOf(account)
                            ) / (10 ** config.get_decimal(symbol))
                            break
                        except tronpy.exceptions.UnknownError as e:
                            logger.debug(
                                f"{account} {symbol} trc20 balance fetch error: {e}"
                            )
                            ret += 1
                    else:
                        raise Exception(
                            f"CONCURRENT_MAX_RETRIES reached while getting trc20 balance of {account}"
                        )

                    stats["balances"][symbol] += trc20_balance

                    if config.SAVE_BALANCES_TO_DB:
                        acc_balance = session.exec(
                            select(Balance).where(
                                Balance.account == account, Balance.symbol == symbol
                            )
                        ).first()
                        if acc_balance:
                            acc_balance.balance = trc20_balance

                        else:
                            acc_balance = Balance()
                            acc_balance.account = account
                            acc_balance.symbol = symbol
                            acc_balance.balance = trc20_balance
                        session.add(acc_balance)
                        session.commit()

                    if trc20_balance > 0:
                        balances_to_collect["trc20"].append(
                            [account, symbol, trc20_balance]
                        )

                #
                # TRX
                #

                while ret := 0 < config.CONCURRENT_MAX_RETRIES:
                    try:
                        trx_balance = ConnectionManager.client().get_account_balance(
                            account
                        )
                        break
                    except tronpy.exceptions.AddressNotFound:
                        trx_balance = Decimal(0)
                        break
                    except tronpy.exceptions.UnknownError as e:
                        logger.debug(f"{account} TRX balance fetch error: {e}")
                        ret += 1
                else:
                    raise Exception(
                        f"CONCURRENT_MAX_RETRIES reached while getting TRX balance of {account}"
                    )

                stats["balances"]["TRX"] += trx_balance

                if config.SAVE_BALANCES_TO_DB:
                    acc_balance = session.exec(
                        select(Balance).where(
                            Balance.account == account, Balance.symbol == "TRX"
                        )
                    ).first()
                    if acc_balance:
                        acc_balance.balance = trx_balance

                    else:
                        acc_balance = Balance()
                        acc_balance.account = account
                        acc_balance.symbol = "TRX"
                        acc_balance.balance = trx_balance
                    session.add(acc_balance)
                    session.commit()

                if trx_balance > 0:
                    if _should_sweep_trx_balance(trx_balance):
                        balances_to_collect["trx"].append([account, trx_balance])
                    else:
                        logger.debug(
                            "%s TRX on %s is below sweep threshold %s; leaving it on account",
                            trx_balance,
                            account,
                            config.TRX_MIN_TRANSFER_THRESHOLD,
                        )

                logger.debug(
                    f"Scanned {index} of {len(accounts)} accounts, found: "
                    + ", ".join([f"{v} {k}" for k, v in stats["balances"].items()])
                )

            except Exception as e:
                logger.exception(f"{account} scan error: {e}")
                stats["exception_num"] += 1

        # Sort trc20 balances by balance in descending order
        balances_to_collect["trc20"].sort(key=lambda x: x[2], reverse=True)
        logger.info("TRC20 queue length: %d" % len(balances_to_collect["trc20"]))
        # Log histogram of TRC20 balances
        bins = [5, 50, 100, 300, 500, 1000, 2000]
        histogram = collections.Counter()
        for _, _, balance in balances_to_collect["trc20"]:
            for b in bins:
                if balance < b:
                    histogram[f"<{b}"] += 1
                    break
            else:
                histogram[">=2000"] += 1
        logger.info(
            "TRC20 balances histogram: "
            + ", ".join([f"{k}: {v}" for k, v in histogram.items()])
        )
        for account, symbol, trc20_balance in balances_to_collect["trc20"]:
            if not is_task_running(
                self,
                "app.tasks.transfer_trc20_from",
                args=[account, symbol],
            ):
                if not is_sweep_allowed(symbol, account):
                    logger.info(
                        "SHKeeper sweep eligibility did not allow periodic "
                        f"{symbol} sweep for {account}; leaving balance."
                    )
                    continue
                transfer_trc20_from(account, symbol)

        # Sort trx balances by balance in descending order
        balances_to_collect["trx"].sort(key=lambda x: x[1], reverse=True)
        for account, trx_balance in balances_to_collect["trx"]:
            if not is_task_running(
                self, "app.tasks.transfer_trc20_from", args=[account]
            ):
                transfer_trx_from(account)

    return stats


@celery.task(bind=True)
@skip_if_running
def vote_for_sr(self, *args, **kwargs):
    logger.info("Checking voting config")
    if not config.SR_VOTES:
        logger.warning("Voting enabled but no config given. Terminating voting task.")
        return
    logger.info(f"Voting config is OK: {config.SR_VOTES}")
    tron_client = ConnectionManager.client()

    energy_delegator_priv, energy_delegator_pub = get_energy_delegator()

    logger.info(f"Checking current votes for {energy_delegator_pub}")
    acc_info = tron_client.get_account(energy_delegator_pub)

    if "votes" in acc_info:
        from .schemas import SrVote

        ta = TypeAdapter(List[SrVote])
        votes = ta.validate_python(acc_info["votes"])

        if config.SR_VOTES == votes:
            logger.info("Already voted according to config. Terminating voting task.")
            return
        else:
            logger.info("Voting config doesn't match previous voting.")
            logger.info("Revoting.")
    else:
        logger.info("Account hasn't voted yet.")
        logger.info("Voting.")

    logger.info(f"Check {energy_delegator_pub} bandwidth")
    need_bw = est_vote_tx_bw_cons(len(config.SR_VOTES))
    logger.info(
        f"Estimated bandwith requirement to vote "
        f"for {len(config.SR_VOTES)} SRs is: {need_bw}"
    )
    if has_free_bw(energy_delegator_pub, need_bw):
        logger.info("Using free bandwidth")
    else:
        logger.info("Available free bandwith points is not enough to vote")
        if config.SR_VOTING_ALLOW_BURN_TRX:
            logger.info("Voting will burn TRX for bandwidth points")
        else:
            logger.warning(
                "Burning TRX for bandwidth points is not allowed. Terminating voting."
            )
            return

    with fee_deposit_spend_guard_for_address(
        energy_delegator_pub,
        reason="sr-vote",
    ):
        unsigned_tx = tron_client.trx.vote_witness(
            energy_delegator_pub,
            *[(v.vote_address, v.vote_count) for v in config.SR_VOTES],
        ).build()
        signed_tx = unsigned_tx.sign(energy_delegator_priv)
        tx_info = signed_tx.broadcast().wait()

    logger.info(f"Voting complete. TX details: {tx_info}")


@celery.task(bind=True)
@skip_if_running
def claim_reward(self, *args, **kwargs):
    # TODO: implement automatic reward claims
    # logger.info("Checking voting config")
    # if not config.SR_VOTES:
    #     logger.warning("Voting enabled but no config given. Terminating voting task.")
    #     return
    # logger.info(f"Voting config is OK: {config.SR_VOTES}")
    # tron_client = ConnectionManager.client()
    # main_acc_keys = query_db2(
    #     'select * from keys where type = "fee_deposit" ', one=True
    # )
    # main_priv_key = PrivateKey(
    #     bytes.fromhex(wallet_encryption.decrypt(main_acc_keys["private"]))
    # )
    # main_publ_key = main_acc_keys["public"]
    # logger.info(f"Checking current votes for {main_publ_key}")
    # acc_info = tron_client.get_account(main_publ_key)
    # # "allowance": 16678,
    # # "latest_withdraw_time": 1752679503000,
    # # once every 24 h
    pass


@celery.on_after_configure.connect
def setup_periodic_tasks(sender: Celery, **kwargs):
    if config.SR_VOTING:
        vote_for_sr.delay()

    if config.PAYOUT_CALLBACK_SWEEP_ENABLED:
        sender.add_periodic_task(
            config.PAYOUT_CALLBACK_SWEEP_PERIOD_SEC,
            dispatch_due_payout_callbacks.s(),
        )

    if config.EXTERNAL_DRAIN_CONFIG:
        from .custom.aml.tasks import sweep_accounts, recheck_transactions

        sender.add_periodic_task(
            config.AML_RESULT_UPDATE_PERIOD, recheck_transactions.s()
        )
        sender.add_periodic_task(config.AML_SWEEP_ACCOUNTS_PERIOD, sweep_accounts.s())
    else:
        sender.add_periodic_task(config.BALANCES_RESCAN_PERIOD, scan_accounts.s())

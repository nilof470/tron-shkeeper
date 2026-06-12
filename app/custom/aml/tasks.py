import time
from sqlmodel import Session, select
from ... import celery
from ...config import config
from .classes import AmlWallet
from ...utils import short_txid

from app.db import engine, query_db
from app.logging import logger
from .models import Transaction
from app.schemas import TronAddress, TronSymbol
from app.utils import skip_if_running
from app.sweep_guard import is_sweep_allowed, is_sweep_gate_active
from app.wallet import Wallet


@celery.task(bind=True)
@skip_if_running
def run_payout_for_tx(self, symbol, account, tx_id):
    if is_sweep_gate_active(symbol):
        from app.tasks import transfer_trc20_from

        return transfer_trc20_from(account, symbol, txid=tx_id)
    wallet = AmlWallet(symbol=symbol)
    if account == wallet.main_account["public"]:
        logger.debug(f"{account} is fee-dopisit, skipping ")
        return False
    results = wallet.payout_for_tx(tx_id, account)
    return results


def _mark_transaction_status(txid, status):
    with Session(engine) as session:
        tx = session.exec(select(Transaction).where(Transaction.tx_id == txid)).first()
        if not tx:
            return False
        tx.status = status
        session.add(tx)
        session.commit()
        session.refresh(tx)
        return True


def queue_guarded_payout_if_allowed(symbol, account, txid):
    if is_sweep_allowed(symbol, account, txid=txid):
        if not _mark_transaction_status(txid, "ready"):
            logger.warning(
                f"Cannot mark guarded {symbol} transaction {short_txid(txid)} "
                f"ready for {account}; refusing to enqueue sweep."
            )
            return False
        run_payout_for_tx.delay(symbol, account, txid)
        return True
    logger.info(
        f"SHKeeper sweep eligibility did not allow guarded {symbol} "
        f"sweep for {account}; skipping legacy AMLBot check."
    )
    return False


@celery.task(bind=True)
@skip_if_running
def check_transaction(self, symbol: TronSymbol, account: TronAddress, txid: str):
    if is_sweep_gate_active(symbol):
        return queue_guarded_payout_if_allowed(symbol, account, txid)

    from .functions import (
        aml_check_transaction,
    )
    result = aml_check_transaction(account, txid)
    if (
        result["result"]
        and result["data"]["status"] == "pending"
        and "uid" in result["data"]
    ):
        status = "rechecking"
        uid = result["data"]["uid"]
        score = -1
    elif (
        result["result"]
        and "riskscore" in result["data"]
        and "uid" in result["data"]
        and result["data"]["status"] == "success"
    ):
        status = "ready"
        score = result["data"]["riskscore"]
        uid = result["data"]["uid"]
    else:
        logger.warning(f"Cannot update the transaction, something wrong - {result}")
        return False

    time.sleep(5)

    with Session(engine) as session:
        pd = session.exec(
            select(Transaction).where(
                Transaction.address == account, Transaction.tx_id == txid
            )
        ).one()
        pd.uid = uid
        pd.score = score
        pd.status = status
        session.add(pd)
        session.commit()
        session.refresh(pd)

    if status == "ready":
        run_payout_for_tx.delay(symbol, account, txid)
        return True


@celery.task(bind=True)
@skip_if_running
def recheck_transaction(self, uid, txid):
    with Session(engine) as session:
        guarded_tx = session.exec(
            select(Transaction).where(Transaction.tx_id == txid)
        ).first()
    if guarded_tx and is_sweep_gate_active(guarded_tx.crypto):
        return queue_guarded_payout_if_allowed(guarded_tx.crypto, guarded_tx.address, txid)
    if not guarded_tx:
        logger.warning(f"Cannot find tx {short_txid(txid)} in DB")
        return False

    from .functions import (
        aml_recheck_transaction,
    )

    result = aml_recheck_transaction(uid, txid)
    if (
        result["result"]
        and result["data"]["status"] == "pending"
        and "uid" in result["data"]
    ):
        status = "rechecking"
        uid = result["data"]["uid"]
        score = -1
    elif (
        result["result"]
        and "riskscore" in result["data"]
        and "uid" in result["data"]
        and result["data"]["status"] == "success"
    ):
        status = "ready"
        score = result["data"]["riskscore"]
        uid = result["data"]["uid"]
    else:
        logger.warning(f"Cannot update the transaction, something wrong - {result}")
        return False

    with Session(engine) as session:
        pd = session.exec(select(Transaction).where(Transaction.tx_id == txid)).first()
        pd.uid = uid
        pd.score = score
        pd.status = status
        session.add(pd)
        session.commit()
        session.refresh(pd)

    if status == "ready":
        run_payout_for_tx.delay(pd.crypto, pd.address, txid)


@celery.task(bind=True)
@skip_if_running
def recheck_transactions(self):
    with Session(engine) as session:
        query_recheck = select(Transaction).where(
            Transaction.ttype == "aml", Transaction.status == "rechecking"
        )
        for tx in session.exec(query_recheck):
            recheck_transaction.delay(tx.uid, tx.tx_id)

        query_pending = select(Transaction).where(
            Transaction.ttype == "aml", Transaction.status == "pending"
        )
        for tx in session.exec(query_pending):
            check_transaction.delay(tx.crypto, tx.address, tx.tx_id)
    return True


@celery.task(bind=True)
@skip_if_running
def sweep_accounts(self):
    accounts = [
        row["public"]
        for row in query_db('SELECT public FROM keys WHERE type = "onetime"')
    ]
    logger.info(f"sweeping {len(accounts)} accounts")
    for account in accounts:
        try:
            #
            # TRC20
            #
            for symbol in [token.symbol for token in config.get_tokens()]:
                wallet = Wallet(symbol=symbol)
                balance = wallet.balance_of(account)
                if not balance:
                    continue
                if balance < config.get_min_transfer_threshold(symbol):
                    logger.info(
                        f"{account} balance {balance} {symbol} is less than minimal transfer"
                        f"threshold of {config.get_min_transfer_threshold(symbol)}, skip sweeping"
                    )
                    continue
                logger.info(f"{account} has balance {balance} {symbol.name}")
                with Session(engine) as session:
                    txs = session.exec(
                        select(Transaction).where(
                            Transaction.address == account,
                            Transaction.crypto == symbol,
                        )
                    ).all()
                    guarded_sweep = is_sweep_gate_active(symbol)
                    for tx in txs:
                        if guarded_sweep and tx.ttype != "aml":
                            logger.debug(
                                f"Skipping guarded {symbol} non-deposit AML row "
                                f"{tx.tx_id} with type {tx.ttype}"
                            )
                            continue
                        if guarded_sweep and not is_sweep_allowed(
                            symbol,
                            account,
                            txid=tx.tx_id,
                        ):
                            logger.info(
                                "SHKeeper sweep eligibility did not allow guarded "
                                f"{symbol} sweep for {account}; leaving balance."
                            )
                            continue
                        if guarded_sweep and not _mark_transaction_status(
                            tx.tx_id,
                            "ready",
                        ):
                            logger.warning(
                                f"Cannot mark guarded {symbol} transaction "
                                f"{short_txid(tx.tx_id)} ready; leaving balance."
                            )
                            continue
                        run_payout_for_tx.delay(symbol, account, tx.tx_id)

            #
            # TRX
            #
            symbol = "TRX"
            balance = Wallet().balance_of(account)
            if not balance:
                continue
            if balance < config.TRX_MIN_TRANSFER_THRESHOLD:
                logger.info(
                    f"{account} balance {balance} {symbol} is less than minimal transfer"
                    f"threshold of {config.TRX_MIN_TRANSFER_THRESHOLD}, skip sweeping"
                )
                continue
            logger.info(f"{account} has balance {balance} {symbol}")
            with Session(engine) as session:
                txs = session.exec(
                    select(Transaction).where(
                        Transaction.address == account,
                        Transaction.crypto == symbol,
                    )
                ).all()
                for tx in txs:
                    run_payout_for_tx.delay(symbol, account, tx.tx_id)

        except Exception as e:
            logger.exception(f"{account} sweep error: {e}")

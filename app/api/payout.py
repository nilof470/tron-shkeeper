from decimal import Decimal

from flask import g, request
from flask import current_app as app
import tronpy


from .. import celery
from ..celery_readiness import usdt_payout_worker_ready
from ..config import config
from ..payout_resources import (
    PayoutResourceError,
    estimate_fee_deposit_resources_for_usdt_payout,
)
from ..tasks import payout as payout_task
from ..tasks import prepare_payout, prepare_multipayout
from . import api
from ..wallet import Wallet
from ..logging import logger
from ..payout_auth import payout_auth_required
from ..payout_execution import PayoutExecutionError, PayoutExecutionStore
from ..payout_observability import record_payout_request_failed


PAYOUT_WORKER_UNAVAILABLE_CODE = "PAYOUT_WORKER_UNAVAILABLE"
PAYOUT_WORKER_UNAVAILABLE_MESSAGE = (
    "TRON USDT payout worker is not ready. "
    "Ensure tron-usdt-payouts consumes tron_usdt_fee_payouts before retrying."
)


def _payout_worker_unavailable_response():
    return {
        "status": "error",
        "code": PAYOUT_WORKER_UNAVAILABLE_CODE,
        "message": PAYOUT_WORKER_UNAVAILABLE_MESSAGE,
        "error": PAYOUT_WORKER_UNAVAILABLE_MESSAGE,
    }


def _payout_error_response(exc, operation):
    record_payout_request_failed(operation, exc.code)
    return {
        "status": "error",
        "code": exc.code,
        "message": str(exc),
    }, exc.status_code


def _payout_body():
    return request.get_json(force=True) or {}


def _preflight_response(execution_id=None):
    try:
        return PayoutExecutionStore.preflight(
            _payout_body(),
            authenticated_consumer=g.payout_consumer,
            execution_id=execution_id,
            endpoint_symbol=g.symbol,
        )
    except PayoutExecutionError as exc:
        return _payout_error_response(exc, "preflight")


def _submit_response(execution_id=None):
    try:
        return PayoutExecutionStore.submit(
            _payout_body(),
            authenticated_consumer=g.payout_consumer,
            execution_id=execution_id,
            endpoint_symbol=g.symbol,
        ), 202
    except PayoutExecutionError as exc:
        return _payout_error_response(exc, "submit")


def _status_response(execution_id):
    try:
        return PayoutExecutionStore.status(
            execution_id,
            authenticated_consumer=g.payout_consumer,
            endpoint_symbol=g.symbol,
        )
    except PayoutExecutionError as exc:
        return _payout_error_response(exc, "status")


@api.post("/payout/preflight")
@payout_auth_required
def payout_execution_preflight():
    return _preflight_response()


@api.post("/payout/submit")
@payout_auth_required
def payout_execution_submit():
    return _submit_response()


@api.get("/payout/status/<execution_id>")
@payout_auth_required
def payout_execution_status(execution_id):
    return _status_response(execution_id)


@api.post("/payout-executions/<execution_id>/preflight")
@payout_auth_required
def payout_execution_v1_preflight(execution_id):
    return _preflight_response(execution_id=execution_id)


@api.post("/payout-executions/<execution_id>")
@payout_auth_required
def payout_execution_v1_submit(execution_id):
    return _submit_response(execution_id=execution_id)


@api.get("/payout-executions/<execution_id>")
@payout_auth_required
def payout_execution_v1_status(execution_id):
    return _status_response(execution_id)


@api.post("/calc-tx-fee/<decimal:amount>")
def calc_tx_fee(amount):
    destination = request.args.get("address")
    if (
        config.TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED
        and g.symbol == "USDT"
        and destination
    ):
        try:
            quote = estimate_fee_deposit_resources_for_usdt_payout(
                destination,
                amount,
            )
        except PayoutResourceError as exc:
            return {
                "status": "error",
                "code": exc.code or "PAYOUT_RESOURCE_UNAVAILABLE",
                "message": str(exc),
            }, 503
        except Exception as exc:
            return {
                "status": "error",
                "code": "INVALID_DESTINATION",
                "message": str(exc),
            }, 400
        resource_quote = quote.to_dict()
        if resource_quote.get("submit_ready") and not usdt_payout_worker_ready():
            resource_quote["submit_ready"] = False
            resource_quote["blocking_code"] = PAYOUT_WORKER_UNAVAILABLE_CODE
            resource_quote["blocking_reason"] = PAYOUT_WORKER_UNAVAILABLE_MESSAGE
        return {"fee": "0", "resource_quote": resource_quote}
    return {"fee": config.TX_FEE}


@api.post("/multipayout")
def multipayout():
    try:
        payout_list = request.get_json(force=True)
    except Exception as e:
        raise Exception(f"Bad JSON in payout list: {e}")

    if not payout_list:
        raise Exception(f"Payout list is empty!")

    for transfer in payout_list:
        try:
            tronpy.keys.to_base58check_address(transfer["dest"])
        except Exception as e:
            raise Exception(f"Bad destination address in {transfer}: {e}")
        try:
            transfer["amount"] = Decimal(transfer["amount"])
        except Exception as e:
            raise Exception(f"Bad amount in {transfer}: {e}")

        if transfer["amount"] <= 0:
            raise Exception(f"Payout amount should be a positive number: {transfer}")

    wallet = Wallet(g.symbol)
    balance = wallet.balance
    need_tokens = sum([transfer["amount"] for transfer in payout_list])
    if balance < need_tokens:
        raise Exception(
            f"Not enough {g.symbol} tokens to make all payouts. "
            f"Has: {balance}, need: {need_tokens}"
        )

    need_currency = len(payout_list) * config.TX_FEE
    trx_balance = Wallet().balance
    if trx_balance < need_currency:
        raise Exception(
            f"Not enough TRX tokens at fee-deposit account {wallet.main_account} to pay payout fees. "
            f"Has: {trx_balance}, need: {need_currency}"
        )

    if "dryrun" in request.args:
        return {
            "currency": {
                "need": need_currency,
                "have": trx_balance,
            },
            "tokens": {
                "need": need_tokens,
                "have": balance,
            },
        }

    prepare_sig = prepare_multipayout.s(payout_list, g.symbol)
    execute_sig = payout_task.s(g.symbol)
    if (
        config.TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED
        and g.symbol == "USDT"
    ):
        if not usdt_payout_worker_ready():
            return _payout_worker_unavailable_response(), 503
        prepare_sig = prepare_sig.set(queue=config.TRON_USDT_PAYOUT_QUEUE)
        execute_sig = execute_sig.set(queue=config.TRON_USDT_PAYOUT_QUEUE)
    task = (prepare_sig | execute_sig).apply_async()
    return {"task_id": task.id}


@api.post("/payout/<to>/<decimal:amount>")
def payout(to, amount):
    try:
        tronpy.keys.to_base58check_address(to)
    except Exception as e:
        raise Exception(f"Bad destination address: {e}")
    if amount <= 0:
        raise Exception("Payout amount should be a positive number")

    prepare_sig = prepare_payout.s(to, amount, g.symbol)
    execute_sig = payout_task.s(g.symbol)
    if (
        config.TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED
        and g.symbol == "USDT"
    ):
        if not usdt_payout_worker_ready():
            return _payout_worker_unavailable_response(), 503
        prepare_sig = prepare_sig.set(queue=config.TRON_USDT_PAYOUT_QUEUE)
        execute_sig = execute_sig.set(queue=config.TRON_USDT_PAYOUT_QUEUE)
    task = (prepare_sig | execute_sig).apply_async()
    return {"task_id": task.id}


@api.post("/task/<id>")
def get_task(id):
    task = celery.AsyncResult(id)
    if isinstance(task.result, Exception):
        return {"status": task.status, "result": task.result.args[0]}
    else:
        return {"status": task.status, "result": task.result}

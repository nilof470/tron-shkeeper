import requests

from .config import config
from .logging import logger


GUARDED_TRON_SYMBOLS = {"USDT"}


def _symbol_value(symbol):
    value = getattr(symbol, "value", symbol)
    return str(value).upper()


def is_sweep_gate_active(symbol):
    return _symbol_value(symbol) in GUARDED_TRON_SYMBOLS


def is_sweep_allowed(symbol, address, txid=None):
    if not is_sweep_gate_active(symbol):
        return True

    payload = {
        "crypto": _symbol_value(symbol),
        "network": "TRON",
        "address": address,
    }
    if txid:
        payload["txid"] = txid

    try:
        response = requests.post(
            f"http://{config.SHKEEPER_HOST}/api/v1/sweep-eligibility",
            headers={"X-Shkeeper-Backend-Key": config.SHKEEPER_BACKEND_KEY},
            json=payload,
            timeout=config.AML_SWEEP_GATE_TIMEOUT_SEC,
        )
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as exc:
        logger.warning(
            "SHKeeper sweep eligibility request failed; refusing guarded sweep: "
            f"symbol={_symbol_value(symbol)} address={address} txid={txid} "
            f"error={exc}"
        )
        return False
    except (TypeError, ValueError) as exc:
        logger.warning(
            "SHKeeper sweep eligibility returned invalid JSON; refusing guarded "
            f"sweep: symbol={_symbol_value(symbol)} address={address} txid={txid} "
            f"error={exc}"
        )
        return False

    if not isinstance(body, dict):
        logger.warning(
            "SHKeeper sweep eligibility returned a non-object response; refusing "
            f"guarded sweep: symbol={_symbol_value(symbol)} address={address} "
            f"txid={txid} response={body!r}"
        )
        return False

    if body.get("decision") == "allow":
        logger.info(
            "SHKeeper sweep eligibility allowed guarded sweep: "
            f"symbol={_symbol_value(symbol)} address={address} txid={txid} "
            f"reason={body.get('reason')}"
        )
        return True

    logger.info(
        "SHKeeper sweep eligibility did not allow guarded sweep: "
        f"symbol={_symbol_value(symbol)} address={address} txid={txid} "
        f"decision={body.get('decision')} reason={body.get('reason')}"
    )
    return False

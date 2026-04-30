#!/usr/bin/env python3
"""Phase 3 helper for a controlled re:Fee live sweep.

This helper prepares a local test wallet controlled by tron-shkeeper, checks
whether it is ready for a USDT sweep, and can run transfer_trc20_from directly.
It never prints the re:Fee API key.
"""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_BASE_DIR = Path("/tmp/tron-shkeeper-phase3-e2e")
DEFAULT_SYMBOL = "USDT"
DEFAULT_RENT_DURATION = "1h"
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_TIMEOUT = 60


def utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sqlite_uri(path: Path) -> str:
    return "sqlite:///" + str(path)


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "***"
    return value[:4] + "..." + value[-4:]


def build_refee_json(environ: dict[str, str]) -> str | None:
    api_key = environ.get("REFEE_API_KEY")
    if not api_key:
        return None
    payload: dict[str, Any] = {
        "api_key": api_key,
        "rent_duration_label": environ.get(
            "REFEE_RENT_DURATION_LABEL", DEFAULT_RENT_DURATION
        ),
        "poll_interval_sec": float(
            environ.get("REFEE_RENT_POLL_INTERVAL_SEC", DEFAULT_POLL_INTERVAL)
        ),
        "timeout_sec": int(environ.get("REFEE_RENT_TIMEOUT_SEC", DEFAULT_TIMEOUT)),
    }
    base_url = environ.get("REFEE_API_BASE_URL")
    if base_url:
        payload["api_base_url"] = base_url
    return json.dumps(payload, separators=(",", ":"))


def configure_environment(base_dir: Path) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("DATABASE", str(base_dir / "database.db"))
    os.environ.setdefault("DB_URI", sqlite_uri(base_dir / "tron.db"))
    os.environ.setdefault("BALANCES_DATABASE", str(base_dir / "trc20balances.db"))
    os.environ.setdefault("DEVMODE_ENCRYPTION_PW", "phase3-e2e")
    os.environ.setdefault("DEVMODE_CELERY_NODELAY", "true")
    os.environ.setdefault("SAVE_BALANCES_TO_DB", "false")
    os.environ.setdefault("ENERGY_SOURCE", "refee")
    os.environ.setdefault("ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT", "false")
    if "REFEE" not in os.environ:
        refee_json = build_refee_json(os.environ)
        if refee_json:
            os.environ["REFEE"] = refee_json


def bootstrap_app(base_dir: Path):
    configure_environment(base_dir)

    from app.wallet_encryption import wallet_encryption

    wallet_encryption.setup_encryption()

    from app import create_app
    from app.db import query_db2

    flask_app = create_app()
    with flask_app.app_context():
        current = query_db2(
            'SELECT value FROM settings WHERE name = "current_server_id"', one=True
        )
        if not current:
            query_db2(
                'INSERT INTO settings (name, value) VALUES ("current_server_id", "0")'
            )
    return flask_app


def json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def get_fee_deposit_address() -> str | None:
    from app.db import query_db2

    row = query_db2('SELECT public FROM keys WHERE type = "fee_deposit"', one=True)
    return None if row is None else row["public"]


def generate_wallet(base_dir: Path, symbol: str) -> int:
    flask_app = bootstrap_app(base_dir)

    with flask_app.app_context():
        from tronpy import Tron
        from app.db import get_db
        from app.wallet_encryption import wallet_encryption

        addresses = Tron().generate_address()
        db = get_db()
        db.execute(
            "INSERT INTO keys (symbol, public, private, type) VALUES (?, ?, ?, 'onetime')",
            (
                symbol,
                addresses["base58check_address"],
                wallet_encryption.encrypt(addresses["private_key"]),
            ),
        )
        db.commit()

        json_print(
            {
                "database": os.environ["DATABASE"],
                "db_uri": os.environ["DB_URI"],
                "symbol": symbol,
                "fee_deposit_address": get_fee_deposit_address(),
                "onetime_address": addresses["base58check_address"],
                "next": [
                    "Send 6-10 USDT-TRC20 to onetime_address.",
                    "Do not send TRX to onetime_address for the clean zero-TRX test.",
                    "Run check-wallet after the deposit is confirmed.",
                ],
            }
        )
    return 0


def wallet_status(base_dir: Path, address: str, symbol: str) -> dict[str, Any]:
    flask_app = bootstrap_app(base_dir)

    with flask_app.app_context():
        import tronpy.exceptions
        from app.config import config
        from app.connection_manager import ConnectionManager
        from app.db import query_db2

        tron_client = ConnectionManager.client()
        key_row = query_db2(
            'SELECT public FROM keys WHERE type = "onetime" AND public = ?',
            (address,),
            one=True,
        )

        contract_address = config.get_contract_address(symbol)
        contract = tron_client.get_contract(contract_address)
        decimals = contract.functions.decimals()
        token_raw = contract.functions.balanceOf(address)
        token_balance = Decimal(token_raw) / (Decimal(10) ** decimals)

        account_active = True
        try:
            trx_balance = tron_client.get_account_balance(address)
            resources = tron_client.get_account_resource(address)
        except tronpy.exceptions.AddressNotFound:
            account_active = False
            trx_balance = Decimal("0")
            resources = {}

        min_threshold = config.get_min_transfer_threshold(symbol)
        fee_deposit = get_fee_deposit_address()
        ready_for_clean_sweep = (
            key_row is not None
            and account_active
            and token_balance > min_threshold
            and trx_balance == 0
        )

        return {
            "address": address,
            "symbol": symbol,
            "database": os.environ["DATABASE"],
            "fee_deposit_address": fee_deposit,
            "private_key_present": key_row is not None,
            "account_active": account_active,
            "trc20_balance": str(token_balance),
            "min_transfer_threshold": str(min_threshold),
            "trx_balance": str(trx_balance),
            "energy_limit": resources.get("EnergyLimit", 0),
            "energy_used": resources.get("EnergyUsed", 0),
            "ready_for_clean_sweep": ready_for_clean_sweep,
            "notes": [
                "ready_for_clean_sweep requires sidecar private key, active account, token balance above threshold, and 0 TRX.",
                "If account_active is false, wait for the USDT deposit confirmation before running sweep.",
            ],
        }


def check_wallet(base_dir: Path, address: str, symbol: str) -> int:
    json_print(wallet_status(base_dir, address, symbol))
    return 0


def run_sweep(
    base_dir: Path,
    address: str,
    symbol: str,
    yes: bool,
    allow_nonzero_trx: bool,
    force: bool,
) -> int:
    before = wallet_status(base_dir, address, symbol)
    if not yes:
        print("Refusing to run sweep without --yes.", file=sys.stderr)
        return 2
    if not force:
        if not before["private_key_present"]:
            print("Refusing: sidecar does not have the onetime private key.", file=sys.stderr)
            return 2
        if not before["account_active"]:
            print("Refusing: account is not active on-chain.", file=sys.stderr)
            return 2
        if Decimal(before["trc20_balance"]) <= Decimal(before["min_transfer_threshold"]):
            print("Refusing: token balance is not above min threshold.", file=sys.stderr)
            return 2
        if Decimal(before["trx_balance"]) != 0 and not allow_nonzero_trx:
            print("Refusing: wallet has non-zero TRX; pass --allow-nonzero-trx to continue.", file=sys.stderr)
            return 2

    flask_app = bootstrap_app(base_dir)
    with flask_app.app_context():
        from app.tasks import transfer_trc20_from

        result = transfer_trc20_from(address, symbol)

    after = wallet_status(base_dir, address, symbol)
    report = {
        "started_at": utc_stamp(),
        "address": address,
        "symbol": symbol,
        "before": before,
        "transfer_result": result,
        "after": after,
        "refee_api_key_present": bool(os.environ.get("REFEE_API_KEY"))
        or bool(os.environ.get("REFEE")),
        "refee_api_key_masked": mask_secret(os.environ.get("REFEE_API_KEY")),
    }
    report_path = base_dir / ("phase3-refee-sweep-" + utc_stamp() + ".json")
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    json_print({"report": str(report_path), "transfer_result": result, "after": after})
    return 0


def show_env(base_dir: Path) -> int:
    configure_environment(base_dir)
    json_print(
        {
            "database": os.environ.get("DATABASE"),
            "db_uri": os.environ.get("DB_URI"),
            "balances_database": os.environ.get("BALANCES_DATABASE"),
            "energy_source": os.environ.get("ENERGY_SOURCE"),
            "refee_json_present": bool(os.environ.get("REFEE")),
            "refee_api_key_present": bool(os.environ.get("REFEE_API_KEY")),
            "refee_api_key_masked": mask_secret(os.environ.get("REFEE_API_KEY")),
            "burn_fallback": os.environ.get(
                "ENERGY_DELEGATION_MODE_ALLOW_BURN_TRX_ON_PAYOUT"
            ),
        }
    )
    return 0


def self_test() -> int:
    assert sqlite_uri(Path("/tmp/example.db")) == "sqlite:////tmp/example.db"
    assert mask_secret("1234567890abcdef") == "1234...cdef"
    assert build_refee_json(
        {
            "REFEE_API_KEY": "secret",
            "REFEE_RENT_DURATION_LABEL": "1h",
            "REFEE_RENT_POLL_INTERVAL_SEC": "2",
            "REFEE_RENT_TIMEOUT_SEC": "60",
        }
    ) == '{"api_key":"secret","rent_duration_label":"1h","poll_interval_sec":2.0,"timeout_sec":60}'
    print("self-test: OK")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 3 re:Fee live e2e helper")
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path(os.getenv("PHASE3_E2E_BASE_DIR", DEFAULT_BASE_DIR)),
        help="Local state directory for the Phase 3 test DB and reports.",
    )
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    sub.add_parser("show-env")
    sub.add_parser("generate-wallet")
    check = sub.add_parser("check-wallet")
    check.add_argument("--address", required=True)
    sweep = sub.add_parser("run-sweep")
    sweep.add_argument("--address", required=True)
    sweep.add_argument("--yes", action="store_true")
    sweep.add_argument("--allow-nonzero-trx", action="store_true")
    sweep.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.command == "self-test":
        return self_test()
    if args.command == "show-env":
        return show_env(args.base_dir)
    if args.command == "generate-wallet":
        return generate_wallet(args.base_dir, args.symbol)
    if args.command == "check-wallet":
        return check_wallet(args.base_dir, args.address, args.symbol)
    if args.command == "run-sweep":
        return run_sweep(
            args.base_dir,
            args.address,
            args.symbol,
            args.yes,
            args.allow_nonzero_trx,
            args.force,
        )
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

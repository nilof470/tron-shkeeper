import ast
import inspect
import textwrap
from decimal import Decimal
from types import SimpleNamespace
import unittest

from app.schemas import TronSymbol, TronTransaction


class FakeWallet:
    def __init__(self, balance, bandwidth):
        self.balance = balance
        self.bandwidth = bandwidth

    def bandwidth_of(self, _address):
        return self.bandwidth


class FakeTransfer:
    txid = "should-not-send"

    def __init__(self, calls):
        self.calls = calls
        self._raw_data = {}

    def build(self):
        return self

    def sign(self, _private_key):
        return self

    def broadcast(self):
        self.calls.append("broadcast")
        return self

    def wait(self):
        return {"receipt": {"net_usage": 1}}


class FakeTrx:
    def __init__(self):
        self.transfer_calls = []
        self.broadcast_calls = []

    def transfer(self, *args):
        self.transfer_calls.append(args)
        return FakeTransfer(self.broadcast_calls)


class FakeTronClient:
    def __init__(self, balance):
        self.balance = balance
        self.trx = FakeTrx()

    def get_account_balance(self, _address):
        return self.balance


class FakeConfig:
    BANDWIDTH_PER_TRX_TRANSFER = 270
    TRX_MIN_TRANSFER_THRESHOLD = Decimal("0.5")


class TrxSweepThresholdTests(unittest.TestCase):
    def test_transfer_trx_from_skips_activation_dust_below_threshold(self):
        from app import tasks

        client = FakeTronClient(Decimal("0.100001"))
        original_config = tasks.config
        original_wallet = tasks.Wallet
        original_connection_manager = tasks.ConnectionManager
        original_query_db2 = tasks.query_db2
        try:
            tasks.config = FakeConfig()
            tasks.Wallet = lambda: FakeWallet(
                balance=Decimal("0.100001"),
                bandwidth=Decimal("600"),
            )
            tasks.ConnectionManager = SimpleNamespace(client=lambda: client)
            tasks.query_db2 = lambda *_args, **_kwargs: {
                "public": "TMAIN",
                "private": "unused",
            }

            result = tasks.transfer_trx_from.run("TONETIME")
        finally:
            tasks.config = original_config
            tasks.Wallet = original_wallet
            tasks.ConnectionManager = original_connection_manager
            tasks.query_db2 = original_query_db2

        self.assertIsNone(result)
        self.assertEqual(client.trx.transfer_calls, [])
        self.assertEqual(client.trx.broadcast_calls, [])

    def test_transfer_trx_from_keeps_real_trx_deposits_eligible(self):
        from app import tasks

        self.assertTrue(tasks._should_sweep_trx_balance(Decimal("0.5")))
        self.assertTrue(tasks._should_sweep_trx_balance(Decimal("1")))
        self.assertFalse(tasks._should_sweep_trx_balance(Decimal("0.499999")))

    def test_auto_sweep_paths_use_trx_threshold(self):
        from app import block_scanner, tasks

        for source in (
            inspect.getsource(block_scanner.BlockScanner.scan),
            inspect.getsource(tasks.scan_accounts),
        ):
            tree = ast.parse(textwrap.dedent(source))
            self.assertTrue(
                any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_should_sweep_trx_balance"
                    for node in ast.walk(tree)
                )
            )

    def test_block_scanner_leaves_trx_dust_without_scheduling_sweep(self):
        from app import block_scanner
        from app import tasks

        onetime = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
        scanner = block_scanner.BlockScanner()
        scanner.download_block = lambda _block_num: {"transactions": [{"txID": "tx1"}]}
        scanner.download_tx_info_by_block_num = lambda _block_num: {}
        scanner.get_watched_accounts = lambda: {onetime}
        scanner.notify_shkeeper = lambda *_args, **_kwargs: None
        scanner.main_account = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"

        sweep_calls = []
        original_parse_tx = block_scanner.parse_tx
        original_transfer_trx_from = tasks.transfer_trx_from
        try:
            block_scanner.parse_tx = lambda *_args, **_kwargs: [
                TronTransaction(
                    status="SUCCESS",
                    txid="tx1",
                    symbol=TronSymbol.TRX,
                    src_addr="TYBKF3YCwS9gwwpyov69hyuht7ATEXADAt",
                    dst_addr=onetime,
                    amount=Decimal("0.100001"),
                    is_trc20=False,
                )
            ]
            tasks.transfer_trx_from = SimpleNamespace(
                delay=lambda address: sweep_calls.append(address)
            )

            self.assertTrue(scanner.scan(123))
        finally:
            block_scanner.parse_tx = original_parse_tx
            tasks.transfer_trx_from = original_transfer_trx_from

        self.assertEqual(sweep_calls, [])

    def test_block_scanner_schedules_real_trx_deposit_sweep(self):
        from app import block_scanner
        from app import tasks

        onetime = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
        scanner = block_scanner.BlockScanner()
        scanner.download_block = lambda _block_num: {"transactions": [{"txID": "tx1"}]}
        scanner.download_tx_info_by_block_num = lambda _block_num: {}
        scanner.get_watched_accounts = lambda: {onetime}
        scanner.notify_shkeeper = lambda *_args, **_kwargs: None
        scanner.main_account = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"

        sweep_calls = []
        original_parse_tx = block_scanner.parse_tx
        original_transfer_trx_from = tasks.transfer_trx_from
        try:
            block_scanner.parse_tx = lambda *_args, **_kwargs: [
                TronTransaction(
                    status="SUCCESS",
                    txid="tx1",
                    symbol=TronSymbol.TRX,
                    src_addr="TYBKF3YCwS9gwwpyov69hyuht7ATEXADAt",
                    dst_addr=onetime,
                    amount=Decimal("0.5"),
                    is_trc20=False,
                )
            ]
            tasks.transfer_trx_from = SimpleNamespace(
                delay=lambda address: sweep_calls.append(address)
            )

            self.assertTrue(scanner.scan(123))
        finally:
            block_scanner.parse_tx = original_parse_tx
            tasks.transfer_trx_from = original_transfer_trx_from

        self.assertEqual(sweep_calls, [onetime])


if __name__ == "__main__":
    unittest.main()

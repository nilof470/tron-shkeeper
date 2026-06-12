from decimal import Decimal
from types import SimpleNamespace
import unittest

from app.schemas import TronSymbol, TronTransaction


ONETIME = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
MAIN = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, json_exc=None):
        self.status_code = status_code
        self.payload = payload or {}
        self.json_exc = json_exc

    def raise_for_status(self):
        if self.status_code >= 400:
            from app import sweep_guard

            raise sweep_guard.requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self.json_exc is not None:
            raise self.json_exc
        return self.payload


class AmlSweepGuardClientTests(unittest.TestCase):
    def setUp(self):
        from app import sweep_guard

        self.sweep_guard = sweep_guard
        self.original_enabled = sweep_guard.config.AML_SWEEP_GATE_ENABLED
        self.original_host = sweep_guard.config.SHKEEPER_HOST
        self.original_key = sweep_guard.config.SHKEEPER_BACKEND_KEY
        self.original_timeout = sweep_guard.config.AML_SWEEP_GATE_TIMEOUT_SEC
        self.original_post = sweep_guard.requests.post
        sweep_guard.config.AML_SWEEP_GATE_ENABLED = True
        sweep_guard.config.SHKEEPER_HOST = "shkeeper.local:5000"
        sweep_guard.config.SHKEEPER_BACKEND_KEY = "backend-secret"
        sweep_guard.config.AML_SWEEP_GATE_TIMEOUT_SEC = 2.5

    def tearDown(self):
        self.sweep_guard.config.AML_SWEEP_GATE_ENABLED = self.original_enabled
        self.sweep_guard.config.SHKEEPER_HOST = self.original_host
        self.sweep_guard.config.SHKEEPER_BACKEND_KEY = self.original_key
        self.sweep_guard.config.AML_SWEEP_GATE_TIMEOUT_SEC = self.original_timeout
        self.sweep_guard.requests.post = self.original_post

    def test_exact_allow_permits_guarded_usdt_sweep(self):
        calls = []

        def post(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse(payload={"decision": "allow", "reason": "aml_approved"})

        self.sweep_guard.requests.post = post

        allowed = self.sweep_guard.is_sweep_allowed("USDT", ONETIME, txid="tx-1")

        self.assertTrue(allowed)
        self.assertEqual(
            calls[0][0],
            "http://shkeeper.local:5000/api/v1/sweep-eligibility",
        )
        self.assertEqual(
            calls[0][1]["headers"],
            {"X-Shkeeper-Backend-Key": "backend-secret"},
        )
        self.assertEqual(calls[0][1]["timeout"], 2.5)
        self.assertEqual(
            calls[0][1]["json"],
            {
                "crypto": "USDT",
                "network": "TRON",
                "address": ONETIME,
                "txid": "tx-1",
            },
        )

    def test_guarded_usdt_still_calls_shkeeper_when_legacy_gate_flag_is_disabled(self):
        self.sweep_guard.config.AML_SWEEP_GATE_ENABLED = False
        calls = []

        def post(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse(payload={"decision": "wait", "reason": "aml_pending"})

        self.sweep_guard.requests.post = post

        self.assertFalse(self.sweep_guard.is_sweep_allowed("USDT", ONETIME))
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][1]["json"],
            {
                "crypto": "USDT",
                "network": "TRON",
                "address": ONETIME,
            },
        )

    def test_non_guarded_symbol_permits_without_calling_shkeeper(self):
        self.sweep_guard.requests.post = lambda *_args, **_kwargs: self.fail(
            "SHKeeper must not be called for non-guarded TRON symbols"
        )

        self.assertTrue(self.sweep_guard.is_sweep_allowed("USDC", ONETIME))

    def test_non_allow_decisions_fail_closed(self):
        for decision in ("wait", "block", "error", None, "ALLOW"):
            with self.subTest(decision=decision):
                self.sweep_guard.requests.post = lambda *_args, **_kwargs: FakeResponse(
                    payload={"decision": decision}
                )

                self.assertFalse(self.sweep_guard.is_sweep_allowed("USDT", ONETIME))

    def test_transport_timeout_invalid_json_and_http_errors_fail_closed(self):
        failures = [
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                self.sweep_guard.requests.Timeout("timeout")
            ),
            lambda *_args, **_kwargs: FakeResponse(json_exc=ValueError("bad json")),
            lambda *_args, **_kwargs: FakeResponse(status_code=403),
            lambda *_args, **_kwargs: FakeResponse(status_code=500),
        ]

        for post in failures:
            with self.subTest(post=post):
                self.sweep_guard.requests.post = post

                self.assertFalse(self.sweep_guard.is_sweep_allowed("USDT", ONETIME))


class AmlSweepGuardIntegrationTests(unittest.TestCase):
    def test_transfer_trc20_from_stops_before_chain_or_fee_wallet_actions_without_allow(self):
        from app import tasks

        events = []
        original_guard = tasks.is_sweep_allowed
        original_connection_manager = tasks.ConnectionManager
        try:
            tasks.is_sweep_allowed = lambda symbol, address, txid=None: events.append(
                ("guard", str(symbol), address, txid)
            ) or False
            tasks.ConnectionManager = SimpleNamespace(
                client=lambda: self.fail("chain client must not be touched before allow")
            )

            result = tasks.transfer_trc20_from.run(ONETIME, "USDT", txid="tx-1")
        finally:
            tasks.is_sweep_allowed = original_guard
            tasks.ConnectionManager = original_connection_manager

        self.assertFalse(result)
        self.assertEqual(events, [("guard", "USDT", ONETIME, "tx-1")])

    def test_live_scanner_does_not_enqueue_guarded_usdt_when_shkeeper_waits(self):
        from app import block_scanner
        from app import tasks

        scanner = block_scanner.BlockScanner()
        scanner.download_block = lambda _block_num: {"transactions": [{"txID": "tx1"}]}
        scanner.download_tx_info_by_block_num = lambda _block_num: {}
        scanner.get_watched_accounts = lambda: {ONETIME}
        scanner.notify_shkeeper = lambda *_args, **_kwargs: None
        scanner.main_account = MAIN

        guard_calls = []
        sweep_calls = []
        original_parse_tx = block_scanner.parse_tx
        original_guard = block_scanner.is_sweep_allowed
        original_transfer_trc20_from = tasks.transfer_trc20_from
        try:
            block_scanner.parse_tx = lambda *_args, **_kwargs: [
                TronTransaction(
                    status="SUCCESS",
                    txid="tx1",
                    symbol=TronSymbol.USDT,
                    src_addr="TYBKF3YCwS9gwwpyov69hyuht7ATEXADAt",
                    dst_addr=ONETIME,
                    amount=Decimal("10"),
                    is_trc20=True,
                )
            ]
            block_scanner.is_sweep_allowed = (
                lambda symbol, address, txid=None: guard_calls.append(
                    (str(symbol), address, txid)
                )
                or False
            )
            tasks.transfer_trc20_from = SimpleNamespace(
                delay=lambda *args, **_kwargs: sweep_calls.append(args)
            )

            self.assertTrue(scanner.scan(123))
        finally:
            block_scanner.parse_tx = original_parse_tx
            block_scanner.is_sweep_allowed = original_guard
            tasks.transfer_trc20_from = original_transfer_trc20_from

        self.assertEqual(guard_calls, [("USDT", ONETIME, "tx1")])
        self.assertEqual(sweep_calls, [])

    def test_periodic_scan_does_not_call_guarded_usdt_sweep_when_shkeeper_blocks(self):
        from app import tasks

        events = []
        original_guard = tasks.is_sweep_allowed
        original_transfer_trc20_from = tasks.transfer_trc20_from
        original_is_task_running = tasks.is_task_running
        original_query_db = tasks.query_db
        original_session = tasks.Session
        original_engine = tasks.engine if hasattr(tasks, "engine") else None
        original_connection_manager = tasks.ConnectionManager
        original_config = tasks.config
        try:
            class FakeSession:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def exec(self, *_args, **_kwargs):
                    return SimpleNamespace(first=lambda: None)

                def add(self, *_args, **_kwargs):
                    return None

                def commit(self, *_args, **_kwargs):
                    return None

            tasks.is_sweep_allowed = lambda symbol, account, txid=None: events.append(
                ("guard", str(symbol), account, txid)
            ) or False
            tasks.transfer_trc20_from = lambda *args: events.append(("sweep", args))
            tasks.is_task_running = lambda *_args, **_kwargs: False
            tasks.query_db = lambda *_args, **_kwargs: [{"public": ONETIME}]
            tasks.Session = lambda _engine: FakeSession()
            tasks.engine = object()
            tasks.config = SimpleNamespace(
                get_tokens=lambda: [SimpleNamespace(symbol=TronSymbol.USDT)],
                get_contract_address=lambda _symbol: "contract",
                get_decimal=lambda _symbol: 6,
                CONCURRENT_MAX_RETRIES=1,
                SAVE_BALANCES_TO_DB=False,
                TRX_MIN_TRANSFER_THRESHOLD=Decimal("0.5"),
            )

            class FakeContract:
                functions = SimpleNamespace(balanceOf=lambda _account: 10_000_000)

            class FakeClient:
                def get_contract(self, _address):
                    return FakeContract()

                def get_account_balance(self, _account):
                    return Decimal("0")

            tasks.ConnectionManager = SimpleNamespace(client=lambda: FakeClient())

            tasks.scan_accounts.run.__wrapped__(tasks.scan_accounts)
        finally:
            tasks.is_sweep_allowed = original_guard
            tasks.transfer_trc20_from = original_transfer_trc20_from
            tasks.is_task_running = original_is_task_running
            tasks.query_db = original_query_db
            tasks.Session = original_session
            if original_engine is not None:
                tasks.engine = original_engine
            tasks.ConnectionManager = original_connection_manager
            tasks.config = original_config

        self.assertEqual(events, [("guard", "USDT", ONETIME, None)])

    def test_legacy_amlbot_payout_path_bypasses_to_shkeeper_guarded_sweep_for_usdt(self):
        from app.custom.aml import tasks as custom_tasks

        events = []
        original_gate = custom_tasks.is_sweep_gate_active
        original_wallet = custom_tasks.AmlWallet
        try:
            custom_tasks.is_sweep_gate_active = lambda symbol: str(symbol) == "USDT"
            custom_tasks.AmlWallet = lambda *_args, **_kwargs: self.fail(
                "legacy AMLBot wallet must not be used for guarded USDT"
            )

            import app.tasks as app_tasks

            original_transfer = app_tasks.transfer_trc20_from
            app_tasks.transfer_trc20_from = (
                lambda account, symbol, txid=None: events.append(
                    ("regular-sweep", account, str(symbol), txid)
                )
                or "sweep-result"
            )
            try:
                result = custom_tasks.run_payout_for_tx.run.__wrapped__(
                    custom_tasks.run_payout_for_tx, "USDT", ONETIME, "tx-1"
                )
            finally:
                app_tasks.transfer_trc20_from = original_transfer
        finally:
            custom_tasks.is_sweep_gate_active = original_gate
            custom_tasks.AmlWallet = original_wallet

        self.assertEqual(result, "sweep-result")
        self.assertEqual(events, [("regular-sweep", ONETIME, "USDT", "tx-1")])

    def test_custom_live_scanner_persists_guarded_usdt_before_waiting_on_shkeeper(self):
        from app import block_scanner
        from app.custom.aml import functions as aml_functions
        from app.custom.aml import tasks as custom_tasks

        scanner = block_scanner.BlockScanner()
        scanner.download_block = lambda _block_num: {"transactions": [{"txID": "tx1"}]}
        scanner.download_tx_info_by_block_num = lambda _block_num: {}
        scanner.get_watched_accounts = lambda: {ONETIME}
        scanner.notify_shkeeper = lambda *_args, **_kwargs: None
        scanner.main_account = MAIN

        events = []
        original_external = block_scanner.config.EXTERNAL_DRAIN_CONFIG
        original_parse_tx = block_scanner.parse_tx
        original_gate = block_scanner.is_sweep_gate_active
        original_allowed = block_scanner.is_sweep_allowed
        original_add = aml_functions.add_transaction_to_db
        original_queue = custom_tasks.queue_guarded_payout_if_allowed
        try:
            block_scanner.config.EXTERNAL_DRAIN_CONFIG = object()
            block_scanner.parse_tx = lambda *_args, **_kwargs: [
                TronTransaction(
                    status="SUCCESS",
                    txid="tx1",
                    symbol=TronSymbol.USDT,
                    src_addr="TYBKF3YCwS9gwwpyov69hyuht7ATEXADAt",
                    dst_addr=ONETIME,
                    amount=Decimal("10"),
                    is_trc20=True,
                )
            ]
            block_scanner.is_sweep_gate_active = lambda symbol: str(symbol) == "USDT"
            block_scanner.is_sweep_allowed = (
                lambda symbol, address, txid=None: events.append(
                    ("guard", str(symbol), address, txid)
                )
                or False
            )
            aml_functions.add_transaction_to_db = (
                lambda *args, **kwargs: events.append(
                    ("add-tx", args, kwargs))
            )
            custom_tasks.queue_guarded_payout_if_allowed = (
                lambda symbol, address, txid=None: events.append(
                    ("queue-guarded", str(symbol), address, txid)
                )
                or False
            )

            self.assertTrue(scanner.scan(123))
        finally:
            block_scanner.config.EXTERNAL_DRAIN_CONFIG = original_external
            block_scanner.parse_tx = original_parse_tx
            block_scanner.is_sweep_gate_active = original_gate
            block_scanner.is_sweep_allowed = original_allowed
            aml_functions.add_transaction_to_db = original_add
            custom_tasks.queue_guarded_payout_if_allowed = original_queue

        self.assertEqual(
            events,
            [
                (
                    "add-tx",
                    ("tx1", ONETIME, Decimal("10"), TronSymbol.USDT),
                    {"enqueue_check": False},
                ),
                ("queue-guarded", "USDT", ONETIME, "tx1"),
            ],
        )

    def test_legacy_amlbot_check_transaction_does_not_call_amlbot_for_guarded_usdt(self):
        from app.custom.aml import functions as aml_functions
        from app.custom.aml import tasks as custom_tasks

        events = []
        original_gate = custom_tasks.is_sweep_gate_active
        original_allowed = custom_tasks.is_sweep_allowed
        original_check = aml_functions.aml_check_transaction
        original_delay = custom_tasks.run_payout_for_tx.delay
        try:
            custom_tasks.is_sweep_gate_active = lambda symbol: str(symbol) == "USDT"
            custom_tasks.is_sweep_allowed = (
                lambda symbol, account, txid=None: events.append(
                    ("guard", str(symbol), account, txid)
                )
                or False
            )
            aml_functions.aml_check_transaction = (
                lambda *_args, **_kwargs: self.fail(
                    "legacy AMLBot check must not run for guarded USDT"
                )
            )
            custom_tasks.run_payout_for_tx.delay = (
                lambda *args, **_kwargs: events.append(("enqueue", args))
            )

            result = custom_tasks.check_transaction.run.__wrapped__(
                custom_tasks.check_transaction,
                "USDT",
                ONETIME,
                "tx-1",
            )
        finally:
            custom_tasks.is_sweep_gate_active = original_gate
            custom_tasks.is_sweep_allowed = original_allowed
            aml_functions.aml_check_transaction = original_check
            custom_tasks.run_payout_for_tx.delay = original_delay

        self.assertFalse(result)
        self.assertEqual(events, [("guard", "USDT", ONETIME, "tx-1")])

    def test_legacy_amlbot_recheck_transaction_does_not_call_amlbot_for_guarded_usdt(self):
        from app.custom.aml import functions as aml_functions
        from app.custom.aml import tasks as custom_tasks

        fake_tx = SimpleNamespace(
            crypto=TronSymbol.USDT,
            address=ONETIME,
            tx_id="tx-1",
        )
        events = []

        class FakeResult:
            def first(self):
                return fake_tx

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def exec(self, *_args, **_kwargs):
                return FakeResult()

        original_gate = custom_tasks.is_sweep_gate_active
        original_allowed = custom_tasks.is_sweep_allowed
        original_recheck = aml_functions.aml_recheck_transaction
        original_session = custom_tasks.Session
        original_delay = custom_tasks.run_payout_for_tx.delay
        try:
            custom_tasks.is_sweep_gate_active = lambda symbol: str(symbol) == "USDT"
            custom_tasks.is_sweep_allowed = (
                lambda symbol, account, txid=None: events.append(
                    ("guard", str(symbol), account, txid)
                )
                or False
            )
            aml_functions.aml_recheck_transaction = (
                lambda *_args, **_kwargs: self.fail(
                    "legacy AMLBot recheck must not run for guarded USDT"
                )
            )
            custom_tasks.Session = lambda _engine: FakeSession()
            custom_tasks.run_payout_for_tx.delay = (
                lambda *args, **_kwargs: events.append(("enqueue", args))
            )

            result = custom_tasks.recheck_transaction.run.__wrapped__(
                custom_tasks.recheck_transaction,
                "uid-1",
                "tx-1",
            )
        finally:
            custom_tasks.is_sweep_gate_active = original_gate
            custom_tasks.is_sweep_allowed = original_allowed
            aml_functions.aml_recheck_transaction = original_recheck
            custom_tasks.Session = original_session
            custom_tasks.run_payout_for_tx.delay = original_delay

        self.assertFalse(result)
        self.assertEqual(events, [("guard", "USDT", ONETIME, "tx-1")])

    def test_queue_guarded_payout_does_not_enqueue_when_status_mark_fails(self):
        from app.custom.aml import tasks as custom_tasks

        events = []
        original_allowed = custom_tasks.is_sweep_allowed
        original_mark = custom_tasks._mark_transaction_status
        original_delay = custom_tasks.run_payout_for_tx.delay
        try:
            custom_tasks.is_sweep_allowed = (
                lambda symbol, account, txid=None: events.append(
                    ("guard", str(symbol), account, txid)
                )
                or True
            )
            custom_tasks._mark_transaction_status = (
                lambda txid, status: events.append(("mark", txid, status)) or False
            )
            custom_tasks.run_payout_for_tx.delay = (
                lambda *args, **_kwargs: events.append(("enqueue", args))
            )

            result = custom_tasks.queue_guarded_payout_if_allowed(
                "USDT", ONETIME, "tx-1"
            )
        finally:
            custom_tasks.is_sweep_allowed = original_allowed
            custom_tasks._mark_transaction_status = original_mark
            custom_tasks.run_payout_for_tx.delay = original_delay

        self.assertFalse(result)
        self.assertEqual(events, [("guard", "USDT", ONETIME, "tx-1"), ("mark", "tx-1", "ready")])

    def test_legacy_amlbot_sweep_accounts_gates_before_enqueue_for_guarded_usdt(self):
        from app.custom.aml import tasks as custom_tasks

        fake_tx = SimpleNamespace(
            crypto=TronSymbol.USDT,
            address=ONETIME,
            tx_id="tx-1",
            ttype="aml",
            status="pending",
        )
        events = []

        class FakeResult:
            def all(self):
                return [fake_tx]

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def exec(self, *_args, **_kwargs):
                return FakeResult()

        class FakeWallet:
            def __init__(self, symbol=None):
                self.symbol = symbol

            def balance_of(self, _account):
                if self.symbol is None:
                    return Decimal("0")
                return Decimal("10")

        original_gate = custom_tasks.is_sweep_gate_active
        original_allowed = custom_tasks.is_sweep_allowed
        original_query_db = custom_tasks.query_db
        original_wallet = custom_tasks.Wallet
        original_session = custom_tasks.Session
        original_config = custom_tasks.config
        original_delay = custom_tasks.run_payout_for_tx.delay
        try:
            custom_tasks.is_sweep_gate_active = lambda symbol: str(symbol) == "USDT"
            custom_tasks.is_sweep_allowed = (
                lambda symbol, account, txid=None: events.append(
                    ("guard", str(symbol), account, txid)
                )
                or False
            )
            custom_tasks.query_db = lambda *_args, **_kwargs: [{"public": ONETIME}]
            custom_tasks.Wallet = FakeWallet
            custom_tasks.Session = lambda _engine: FakeSession()
            custom_tasks.config = SimpleNamespace(
                get_tokens=lambda: [SimpleNamespace(symbol=TronSymbol.USDT)],
                get_min_transfer_threshold=lambda _symbol: Decimal("1"),
                TRX_MIN_TRANSFER_THRESHOLD=Decimal("0.5"),
            )
            custom_tasks.run_payout_for_tx.delay = (
                lambda *args, **_kwargs: events.append(("enqueue", args))
            )

            custom_tasks.sweep_accounts.run.__wrapped__(custom_tasks.sweep_accounts)
        finally:
            custom_tasks.is_sweep_gate_active = original_gate
            custom_tasks.is_sweep_allowed = original_allowed
            custom_tasks.query_db = original_query_db
            custom_tasks.Wallet = original_wallet
            custom_tasks.Session = original_session
            custom_tasks.config = original_config
            custom_tasks.run_payout_for_tx.delay = original_delay

        self.assertEqual(events, [("guard", "USDT", ONETIME, "tx-1")])

    def test_legacy_amlbot_sweep_accounts_skips_guarded_usdt_non_aml_transactions(self):
        from app.custom.aml import tasks as custom_tasks

        fake_tx = SimpleNamespace(
            crypto=TronSymbol.USDT,
            address=ONETIME,
            tx_id="tx-from-fee",
            ttype="from_fee",
            status="skipped",
        )
        events = []

        class FakeResult:
            def all(self):
                return [fake_tx]

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def exec(self, *_args, **_kwargs):
                return FakeResult()

        class FakeWallet:
            def __init__(self, symbol=None):
                self.symbol = symbol

            def balance_of(self, _account):
                if self.symbol is None:
                    return Decimal("0")
                return Decimal("10")

        original_gate = custom_tasks.is_sweep_gate_active
        original_allowed = custom_tasks.is_sweep_allowed
        original_query_db = custom_tasks.query_db
        original_wallet = custom_tasks.Wallet
        original_session = custom_tasks.Session
        original_config = custom_tasks.config
        original_delay = custom_tasks.run_payout_for_tx.delay
        try:
            custom_tasks.is_sweep_gate_active = lambda symbol: str(symbol) == "USDT"
            custom_tasks.is_sweep_allowed = (
                lambda symbol, account, txid=None: events.append(
                    ("guard", str(symbol), account, txid)
                )
                or True
            )
            custom_tasks.query_db = lambda *_args, **_kwargs: [{"public": ONETIME}]
            custom_tasks.Wallet = FakeWallet
            custom_tasks.Session = lambda _engine: FakeSession()
            custom_tasks.config = SimpleNamespace(
                get_tokens=lambda: [SimpleNamespace(symbol=TronSymbol.USDT)],
                get_min_transfer_threshold=lambda _symbol: Decimal("1"),
                TRX_MIN_TRANSFER_THRESHOLD=Decimal("0.5"),
            )
            custom_tasks.run_payout_for_tx.delay = (
                lambda *args, **_kwargs: events.append(("enqueue", args))
            )

            custom_tasks.sweep_accounts.run.__wrapped__(custom_tasks.sweep_accounts)
        finally:
            custom_tasks.is_sweep_gate_active = original_gate
            custom_tasks.is_sweep_allowed = original_allowed
            custom_tasks.query_db = original_query_db
            custom_tasks.Wallet = original_wallet
            custom_tasks.Session = original_session
            custom_tasks.config = original_config
            custom_tasks.run_payout_for_tx.delay = original_delay

        self.assertEqual(events, [])

    def test_legacy_amlbot_sweep_accounts_retries_ready_guarded_usdt(self):
        from app.custom.aml import tasks as custom_tasks

        fake_tx = SimpleNamespace(
            crypto=TronSymbol.USDT,
            address=ONETIME,
            tx_id="tx-ready",
            ttype="aml",
            status="ready",
        )
        events = []

        class FakeResult:
            def all(self):
                return [fake_tx]

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def exec(self, *_args, **_kwargs):
                return FakeResult()

        class FakeWallet:
            def __init__(self, symbol=None):
                self.symbol = symbol

            def balance_of(self, _account):
                if self.symbol is None:
                    return Decimal("0")
                return Decimal("10")

        original_gate = custom_tasks.is_sweep_gate_active
        original_allowed = custom_tasks.is_sweep_allowed
        original_mark = custom_tasks._mark_transaction_status
        original_query_db = custom_tasks.query_db
        original_wallet = custom_tasks.Wallet
        original_session = custom_tasks.Session
        original_config = custom_tasks.config
        original_delay = custom_tasks.run_payout_for_tx.delay
        try:
            custom_tasks.is_sweep_gate_active = lambda symbol: str(symbol) == "USDT"
            custom_tasks.is_sweep_allowed = (
                lambda symbol, account, txid=None: events.append(
                    ("guard", str(symbol), account, txid)
                )
                or True
            )
            custom_tasks._mark_transaction_status = (
                lambda txid, status: events.append(("mark", txid, status)) or True
            )
            custom_tasks.query_db = lambda *_args, **_kwargs: [{"public": ONETIME}]
            custom_tasks.Wallet = FakeWallet
            custom_tasks.Session = lambda _engine: FakeSession()
            custom_tasks.config = SimpleNamespace(
                get_tokens=lambda: [SimpleNamespace(symbol=TronSymbol.USDT)],
                get_min_transfer_threshold=lambda _symbol: Decimal("1"),
                TRX_MIN_TRANSFER_THRESHOLD=Decimal("0.5"),
            )
            custom_tasks.run_payout_for_tx.delay = (
                lambda *args, **_kwargs: events.append(("enqueue", args))
            )

            custom_tasks.sweep_accounts.run.__wrapped__(custom_tasks.sweep_accounts)
        finally:
            custom_tasks.is_sweep_gate_active = original_gate
            custom_tasks.is_sweep_allowed = original_allowed
            custom_tasks._mark_transaction_status = original_mark
            custom_tasks.query_db = original_query_db
            custom_tasks.Wallet = original_wallet
            custom_tasks.Session = original_session
            custom_tasks.config = original_config
            custom_tasks.run_payout_for_tx.delay = original_delay

        self.assertEqual(
            events,
            [
                ("guard", "USDT", ONETIME, "tx-ready"),
                ("mark", "tx-ready", "ready"),
                ("enqueue", (TronSymbol.USDT, ONETIME, "tx-ready")),
            ],
        )

if __name__ == "__main__":
    unittest.main()

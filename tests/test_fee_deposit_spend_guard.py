from contextlib import contextmanager
from decimal import Decimal
import importlib
import sqlite3
import sys
import unittest


FEE_DEPOSIT = "TRfonfrf1AqFzXqJTpad8Tz4EzvCBhZe5k"
ONETIME = "TY4ZLVFpNhpozeWYSqWpcQjv6vntfHnjA7"
TEST_DATABASE = "/private/tmp/tron-shkeeper-fee-deposit-spend-guard.db"


def prepare_database():
    db = sqlite3.connect(TEST_DATABASE)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS keys (
            symbol TEXT,
            public TEXT,
            private TEXT,
            type TEXT
        )
        """
    )
    db.execute("DELETE FROM keys")
    db.execute(
        "INSERT INTO keys (symbol, public, private, type) VALUES (?, ?, ?, ?)",
        ("_", FEE_DEPOSIT, "EXTERNALLY_MANAGED", "fee_deposit"),
    )
    db.commit()
    db.close()

    from app.config import config

    config.DATABASE = TEST_DATABASE
    for module_name in [
        "app.fee_deposit_spend_guard",
        "app.wallet",
        "app.tasks",
    ]:
        sys.modules.pop(module_name, None)


class FakeRedisLock:
    def __init__(self, events):
        self.events = events

    def acquire(self, blocking=True):
        self.events.append(("redis-acquire", blocking))
        return True

    def release(self):
        self.events.append(("redis-release",))


class FakeRedis:
    def __init__(self, events):
        self.events = events

    def lock(self, name, **kwargs):
        self.events.append(("redis-lock", name, kwargs))
        return FakeRedisLock(self.events)


class FakeBroadcast:
    txid = "tx-1"

    def wait(self):
        return {"receipt": {"result": "SUCCESS"}}


class FakeTx:
    txid = "tx-1"

    def __init__(self, events):
        self.events = events
        self._raw_data = {"timestamp": 1}

    def build(self):
        self.events.append(("build",))
        return self

    def sign(self, _private_key):
        self.events.append(("sign",))
        return self

    def inspect(self):
        self.events.append(("inspect",))

    def broadcast(self):
        self.events.append(("broadcast",))
        return FakeBroadcast()


class FakeTrx:
    def __init__(self, events):
        self.events = events

    def transfer(self, src, dst, amount):
        self.events.append(("transfer", src, dst, amount))
        return FakeTx(self.events)

    def delegate_resource(self, owner, receiver, balance, resource):
        self.events.append(("delegate_resource", owner, receiver, balance, resource))
        return FakeTx(self.events)

    def undelegate_resource(self, owner, receiver, balance, resource):
        self.events.append(("undelegate_resource", owner, receiver, balance, resource))
        return FakeTx(self.events)


class FakeProvider:
    def make_request(self, _path, _payload):
        return {"max_size": 10_000_000}


class FakeTronClient:
    def __init__(self, events):
        self.events = events
        self.trx = FakeTrx(events)
        self.provider = FakeProvider()

    def get_account_balance(self, address):
        self.events.append(("balance", address))
        return Decimal("100")

    def get_account_resource(self, address):
        self.events.append(("resources", address))
        return {"EnergyLimit": 10, "EnergyUsed": 0}

    def get_delegated_resource_v2(self, fromAddr, toAddr):
        self.events.append(("delegated_resource", fromAddr, toAddr))
        return {
            "delegatedResource": [
                {
                    "from": fromAddr,
                    "to": toAddr,
                    "frozen_balance_for_energy": 1_000_000,
                }
            ]
        }


class FeeDepositSpendGuardTests(unittest.TestCase):
    def setUp(self):
        prepare_database()
        self.guard = importlib.import_module("app.fee_deposit_spend_guard")
        from app.config import config

        self.config = config
        self.original_enabled = config.TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED
        self.original_redis_host = config.REDIS_HOST
        config.TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED = True
        config.REDIS_HOST = "redis.local"

    def tearDown(self):
        self.config.TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED = (
            self.original_enabled
        )
        self.config.REDIS_HOST = self.original_redis_host

    def patch_fake_redis(self, events):
        original_from_url = self.guard.redis.Redis.from_url
        self.guard.redis.Redis.from_url = lambda *_args, **_kwargs: FakeRedis(events)
        return original_from_url

    def test_fee_deposit_guard_is_reentrant_on_same_lock(self):
        events = []
        original_from_url = self.patch_fake_redis(events)
        try:
            with self.guard.fee_deposit_spend_guard_for_address(
                FEE_DEPOSIT,
                reason="outer",
            ):
                with self.guard.fee_deposit_spend_guard_for_address(
                    FEE_DEPOSIT,
                    reason="inner",
                ):
                    events.append(("inside", self.guard._fee_deposit_lock_depth.get()))
        finally:
            self.guard.redis.Redis.from_url = original_from_url

        self.assertEqual(
            [event[0] for event in events],
            ["redis-lock", "redis-acquire", "inside", "redis-release"],
        )
        self.assertEqual(events[2], ("inside", 2))

    def test_wallet_transfer_locks_fee_deposit_source_before_signing(self):
        wallet_module = importlib.import_module("app.wallet")
        events = []
        original_from_url = self.patch_fake_redis(events)
        try:
            wallet = wallet_module.Wallet.__new__(wallet_module.Wallet)
            wallet.symbol = "USDT"
            wallet.main_account = {"public": FEE_DEPOSIT}

            def build_signed_transfer(_dst, _amount, src_address=None):
                events.append(
                    ("build-depth", self.guard._fee_deposit_lock_depth.get())
                )
                return FakeTx(events)

            wallet.build_signed_transfer = build_signed_transfer
            wallet.broadcast_signed_transfer = lambda _tx: events.append(
                ("broadcast-depth", self.guard._fee_deposit_lock_depth.get())
            ) or {"receipt": {"result": "SUCCESS"}}
            wallet.transfer_result = lambda *_args: {"status": "success"}

            self.assertEqual(wallet.transfer(ONETIME, Decimal("1"))["status"], "success")
        finally:
            self.guard.redis.Redis.from_url = original_from_url

        self.assertIn(("build-depth", 1), events)
        self.assertIn(("broadcast-depth", 1), events)

    def test_trx_fee_funding_uses_fee_deposit_guard(self):
        tasks = importlib.import_module("app.tasks")
        events = []
        original_guard = tasks.fee_deposit_spend_guard_for_address

        @contextmanager
        def fake_guard(address, reason=None):
            events.append(("guard-enter", address, reason))
            yield
            events.append(("guard-exit", address, reason))

        tasks.fee_deposit_spend_guard_for_address = fake_guard
        try:
            result = tasks._fund_onetime_for_trc20_burn(
                FakeTronClient(events),
                FEE_DEPOSIT,
                object(),
                ONETIME,
                Decimal("10"),
                "USDT",
                Decimal("1"),
            )
        finally:
            tasks.fee_deposit_spend_guard_for_address = original_guard

        self.assertTrue(result[0])
        self.assertEqual(
            events[0],
            ("guard-enter", FEE_DEPOSIT, "trc20-sweep-fee-funding"),
        )
        self.assertEqual(events[-1], ("guard-exit", FEE_DEPOSIT, "trc20-sweep-fee-funding"))

    def test_staking_provider_delegation_uses_guard_when_energy_wallet_is_fee_deposit(self):
        staking_module = importlib.import_module("app.resource_providers.staking")
        events = []
        original_guard = staking_module.fee_deposit_spend_guard_for_address
        original_delegator = staking_module.get_energy_delegator

        @contextmanager
        def fake_guard(address, reason=None):
            events.append(("guard-enter", address, reason))
            yield
            events.append(("guard-exit", address, reason))

        staking_module.fee_deposit_spend_guard_for_address = fake_guard
        staking_module.get_energy_delegator = lambda: (object(), FEE_DEPOSIT)
        try:
            provider = staking_module.StakingEnergyProvider(
                tron_client=FakeTronClient(events)
            )
            ok = provider.acquire_energy(
                ONETIME,
                1,
                {"TotalEnergyWeight": 1, "TotalEnergyLimit": 1},
                minimum_energy_required=1,
            )
        finally:
            staking_module.fee_deposit_spend_guard_for_address = original_guard
            staking_module.get_energy_delegator = original_delegator

        self.assertTrue(ok)
        self.assertIn(
            ("guard-enter", FEE_DEPOSIT, "staking-provider-delegate-energy"),
            events,
        )

    def test_undelegate_energy_uses_guard_when_energy_wallet_is_fee_deposit(self):
        tasks = importlib.import_module("app.tasks")
        events = []
        original_guard = tasks.fee_deposit_spend_guard_for_address
        original_delegator = tasks.get_energy_delegator
        original_client = tasks.ConnectionManager.client

        @contextmanager
        def fake_guard(address, reason=None):
            events.append(("guard-enter", address, reason))
            yield
            events.append(("guard-exit", address, reason))

        tasks.fee_deposit_spend_guard_for_address = fake_guard
        tasks.get_energy_delegator = lambda: (object(), FEE_DEPOSIT)
        tasks.ConnectionManager.client = lambda: FakeTronClient(events)
        try:
            tasks.undelegate_energy.run(ONETIME)
        finally:
            tasks.fee_deposit_spend_guard_for_address = original_guard
            tasks.get_energy_delegator = original_delegator
            tasks.ConnectionManager.client = original_client

        self.assertIn(
            ("guard-enter", FEE_DEPOSIT, "energy-undelegate"),
            events,
        )
        guard_enter = events.index(("guard-enter", FEE_DEPOSIT, "energy-undelegate"))
        undelegate_call = next(
            index
            for index, event in enumerate(events)
            if event[0] == "undelegate_resource"
        )
        self.assertLess(guard_enter, undelegate_call)

    def test_staking_api_delegate_uses_guard_when_energy_wallet_is_fee_deposit(self):
        staking_api = importlib.import_module("app.api.staking")
        events = []
        original_guard = staking_api.fee_deposit_spend_guard_for_address
        original_delegator = staking_api.get_energy_delegator
        original_client = staking_api.ConnectionManager.client

        @contextmanager
        def fake_guard(address, reason=None):
            events.append(("guard-enter", address, reason))
            yield
            events.append(("guard-exit", address, reason))

        staking_api.fee_deposit_spend_guard_for_address = fake_guard
        staking_api.get_energy_delegator = lambda: (object(), FEE_DEPOSIT)
        staking_api.ConnectionManager.client = lambda: FakeTronClient(events)
        try:
            result = staking_api.delegate(ONETIME, 1, "ENERGY")
        finally:
            staking_api.fee_deposit_spend_guard_for_address = original_guard
            staking_api.get_energy_delegator = original_delegator
            staking_api.ConnectionManager.client = original_client

        self.assertEqual(result["receipt"]["result"], "SUCCESS")
        self.assertIn(("guard-enter", FEE_DEPOSIT, "staking-delegate"), events)
        guard_enter = events.index(("guard-enter", FEE_DEPOSIT, "staking-delegate"))
        delegate_call = next(
            index for index, event in enumerate(events) if event[0] == "delegate_resource"
        )
        self.assertLess(guard_enter, delegate_call)

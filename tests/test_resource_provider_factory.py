from types import SimpleNamespace
import unittest


class ResourceProviderFactoryTests(unittest.TestCase):
    def test_energy_factory_returns_refee_provider(self):
        from app.resource_providers import factory
        from app.resource_providers.refee import RefeeProvider

        original_config = factory.config
        try:
            factory.config = SimpleNamespace(ENERGY_PROVIDER="refee")
            provider = factory.get_energy_provider(tron_client=object())
        finally:
            factory.config = original_config

        self.assertIsInstance(provider, RefeeProvider)

    def test_energy_factory_returns_profeex_provider(self):
        from app.resource_providers import factory
        from app.resource_providers.profeex import ProfeeXProvider

        original_config = factory.config
        try:
            factory.config = SimpleNamespace(ENERGY_PROVIDER="profeex")
            provider = factory.get_energy_provider(tron_client=object())
        finally:
            factory.config = original_config

        self.assertIsInstance(provider, ProfeeXProvider)

    def test_energy_factory_returns_staking_provider_by_default(self):
        from app.resource_providers import factory
        from app.resource_providers.staking import StakingEnergyProvider

        original_config = factory.config
        try:
            factory.config = SimpleNamespace(ENERGY_PROVIDER="staking")
            provider = factory.get_energy_provider(tron_client=object())
        finally:
            factory.config = original_config

        self.assertIsInstance(provider, StakingEnergyProvider)

    def test_bandwidth_factory_returns_none_when_disabled(self):
        from app.resource_providers import factory

        original_config = factory.config
        try:
            factory.config = SimpleNamespace(BANDWIDTH_PROVIDER="disabled")
            provider = factory.get_bandwidth_provider(tron_client=object())
        finally:
            factory.config = original_config

        self.assertIsNone(provider)

    def test_bandwidth_factory_returns_refee_provider(self):
        from app.resource_providers import factory
        from app.resource_providers.refee import RefeeProvider

        original_config = factory.config
        try:
            factory.config = SimpleNamespace(BANDWIDTH_PROVIDER="refee")
            provider = factory.get_bandwidth_provider(tron_client=object())
        finally:
            factory.config = original_config

        self.assertIsInstance(provider, RefeeProvider)

    def test_bandwidth_factory_returns_profeex_provider(self):
        from app.resource_providers import factory
        from app.resource_providers.profeex import ProfeeXBandwidthProvider

        original_config = factory.config
        try:
            factory.config = SimpleNamespace(BANDWIDTH_PROVIDER="profeex")
            provider = factory.get_bandwidth_provider(tron_client=object())
        finally:
            factory.config = original_config

        self.assertIsInstance(provider, ProfeeXBandwidthProvider)

    def test_bandwidth_factory_raises_for_unknown_provider(self):
        from app.resource_providers import factory

        original_config = factory.config
        try:
            factory.config = SimpleNamespace(BANDWIDTH_PROVIDER="unexpected")
            with self.assertRaises(ValueError):
                factory.get_bandwidth_provider(tron_client=object())
        finally:
            factory.config = original_config


if __name__ == "__main__":
    unittest.main()

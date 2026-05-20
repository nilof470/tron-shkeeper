import unittest

from pydantic import ValidationError

from app.config import Settings
from app.profeex import ProfeeXConfig


class ResourceProviderConfigTests(unittest.TestCase):
    def test_refee_required_for_refee_energy_provider(self):
        with self.assertRaisesRegex(
            ValidationError,
            "REFEE must be configured when ENERGY_PROVIDER='refee'",
        ):
            Settings(ENERGY_PROVIDER="refee", BANDWIDTH_PROVIDER="disabled")

    def test_refee_required_for_refee_bandwidth_provider(self):
        with self.assertRaisesRegex(
            ValidationError,
            "REFEE must be configured when BANDWIDTH_PROVIDER='refee'",
        ):
            Settings(ENERGY_PROVIDER="staking", BANDWIDTH_PROVIDER="refee")

    def test_profeex_required_for_profeex_bandwidth_provider(self):
        with self.assertRaisesRegex(
            ValidationError,
            "PROFEEX must be configured when BANDWIDTH_PROVIDER='profeex'",
        ):
            Settings(ENERGY_PROVIDER="staking", BANDWIDTH_PROVIDER="profeex")

    def test_staking_energy_with_refee_bandwidth_provider_is_valid(self):
        settings = Settings(
            ENERGY_PROVIDER="staking",
            BANDWIDTH_PROVIDER="refee",
            REFEE='{"api_key":"secret"}',
        )

        self.assertEqual(settings.ENERGY_PROVIDER, "staking")
        self.assertEqual(settings.BANDWIDTH_PROVIDER, "refee")

    def test_staking_energy_with_profeex_bandwidth_provider_is_valid(self):
        settings = Settings(
            ENERGY_PROVIDER="staking",
            BANDWIDTH_PROVIDER="profeex",
            PROFEEX='{"api_key":"secret"}',
        )

        self.assertEqual(settings.ENERGY_PROVIDER, "staking")
        self.assertEqual(settings.BANDWIDTH_PROVIDER, "profeex")

    def test_refee_energy_with_disabled_bandwidth_provider_is_valid(self):
        settings = Settings(
            ENERGY_PROVIDER="refee",
            BANDWIDTH_PROVIDER="disabled",
            REFEE='{"api_key":"secret"}',
        )

        self.assertEqual(settings.ENERGY_PROVIDER, "refee")
        self.assertEqual(settings.BANDWIDTH_PROVIDER, "disabled")

    def test_profeex_is_not_valid_energy_provider_yet(self):
        with self.assertRaises(ValidationError):
            Settings(ENERGY_PROVIDER="profeex")

    def test_profeex_config_rejects_non_https_api_base_url(self):
        with self.assertRaises(ValidationError):
            ProfeeXConfig(
                api_key="secret",
                api_base_url="http://api.profeex.test/api/v1",
            )

    def test_profeex_config_rejects_empty_api_key(self):
        with self.assertRaises(ValidationError):
            ProfeeXConfig(api_key="")

    def test_profeex_config_rejects_bandwidth_min_below_api_minimum(self):
        with self.assertRaises(ValidationError):
            ProfeeXConfig(api_key="secret", min_bandwidth_order_amount=349)

    def test_profeex_config_rejects_bandwidth_max_above_api_maximum(self):
        with self.assertRaises(ValidationError):
            ProfeeXConfig(api_key="secret", max_bandwidth_order_amount=10_001)


if __name__ == "__main__":
    unittest.main()

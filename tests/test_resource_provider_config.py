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

    def test_profeex_required_for_profeex_energy_provider(self):
        with self.assertRaisesRegex(
            ValidationError,
            "PROFEEX must be configured when ENERGY_PROVIDER='profeex'",
        ):
            Settings(ENERGY_PROVIDER="profeex", BANDWIDTH_PROVIDER="disabled")

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

    def test_profeex_energy_provider_is_valid_when_configured(self):
        settings = Settings(
            ENERGY_PROVIDER="profeex",
            BANDWIDTH_PROVIDER="disabled",
            PROFEEX='{"api_key":"secret"}',
        )

        self.assertEqual(settings.ENERGY_PROVIDER, "profeex")
        self.assertEqual(settings.PROFEEX.fixed_energy_order_amount, 65_000)
        self.assertEqual(settings.PROFEEX.fixed_bandwidth_order_amount, 350)

    def test_usdt_payout_resource_provisioning_requires_profeex_estimator_config(self):
        with self.assertRaisesRegex(
            ValidationError,
            "PROFEEX must be configured when "
            "TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED=true",
        ):
            Settings(TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED=True)

    def test_usdt_payout_resource_provisioning_is_valid_when_profeex_configured(self):
        settings = Settings(
            TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED=True,
            PROFEEX='{"api_key":"secret"}',
        )

        self.assertTrue(settings.TRON_USDT_PAYOUT_RESOURCE_PROVISIONING_ENABLED)
        self.assertEqual(settings.TRON_USDT_PAYOUT_QUEUE, "tron_usdt_fee_payouts")

    def test_profeex_config_rejects_non_https_api_base_url(self):
        with self.assertRaises(ValidationError):
            ProfeeXConfig(
                api_key="secret",
                api_base_url="http://api.profeex.test/api/v1",
            )

    def test_profeex_config_rejects_empty_api_key(self):
        with self.assertRaises(ValidationError):
            ProfeeXConfig(api_key="")

    def test_profeex_config_rejects_energy_fixed_amount_below_provider_minimum(self):
        with self.assertRaises(ValidationError):
            ProfeeXConfig(api_key="secret", fixed_energy_order_amount=64_284)

    def test_profeex_config_rejects_energy_fixed_amount_above_provider_maximum(self):
        with self.assertRaises(ValidationError):
            ProfeeXConfig(api_key="secret", fixed_energy_order_amount=3_000_001)

    def test_profeex_config_rejects_bandwidth_fixed_amount_below_provider_minimum(self):
        with self.assertRaises(ValidationError):
            ProfeeXConfig(api_key="secret", fixed_bandwidth_order_amount=349)

    def test_profeex_config_rejects_bandwidth_fixed_amount_above_provider_maximum(self):
        with self.assertRaises(ValidationError):
            ProfeeXConfig(api_key="secret", fixed_bandwidth_order_amount=10_001)

    def test_profeex_config_rejects_removed_bandwidth_min_max_keys(self):
        with self.assertRaises(ValidationError):
            ProfeeXConfig(
                api_key="secret",
                min_bandwidth_order_amount=350,
                max_bandwidth_order_amount=10_000,
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from .base import BandwidthProvider, EnergyProvider
from .profeex import ProfeeXBandwidthProvider, ProfeeXProvider
from .refee import RefeeProvider
from .staking import StakingEnergyProvider
from ..config import config


def get_energy_provider_by_name(name: str, tron_client=None) -> EnergyProvider:
    if name == "refee":
        return RefeeProvider(tron_client=tron_client)
    if name == "profeex":
        return ProfeeXProvider(tron_client=tron_client)
    if name == "staking":
        return StakingEnergyProvider(tron_client=tron_client)
    raise ValueError(f"Unknown ENERGY_PROVIDER={name!r}")


def get_energy_provider(tron_client=None) -> EnergyProvider:
    if config.ENERGY_PROVIDER == "refee":
        return get_energy_provider_by_name("refee", tron_client=tron_client)
    if config.ENERGY_PROVIDER == "profeex":
        return get_energy_provider_by_name("profeex", tron_client=tron_client)
    return StakingEnergyProvider(tron_client=tron_client)


def get_bandwidth_provider_by_name(
    name: str, tron_client=None
) -> BandwidthProvider | None:
    if name == "disabled":
        return None
    if name == "refee":
        return RefeeProvider(tron_client=tron_client)
    if name == "profeex":
        return ProfeeXBandwidthProvider(tron_client=tron_client)
    raise ValueError(f"Unknown BANDWIDTH_PROVIDER={name!r}")


def get_bandwidth_provider(tron_client=None) -> BandwidthProvider | None:
    if config.BANDWIDTH_PROVIDER == "disabled":
        return None
    if config.BANDWIDTH_PROVIDER == "refee":
        return get_bandwidth_provider_by_name("refee", tron_client=tron_client)
    if config.BANDWIDTH_PROVIDER == "profeex":
        return get_bandwidth_provider_by_name("profeex", tron_client=tron_client)
    raise ValueError(f"Unknown BANDWIDTH_PROVIDER={config.BANDWIDTH_PROVIDER!r}")

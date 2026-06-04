from __future__ import annotations

from typing import Protocol


class EnergyProvider(Protocol):
    def acquire_energy(
        self,
        receiver: str,
        energy_to_provision: int,
        account_resource: dict,
        *,
        minimum_energy_required: int | None = None,
        strict_minimum_required: bool = False,
    ) -> bool:
        """Make enough TRON energy available for receiver."""

    def release_energy(self, receiver: str) -> None:
        """Release provider-owned energy resources when the provider requires it."""


class BandwidthProvider(Protocol):
    def acquire_bandwidth(self, receiver: str, bandwidth_required: int) -> bool:
        """Make enough TRON bandwidth available for receiver."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, SecretStr


class RefeeConfig(BaseModel):
    api_base_url: str = "https://api.refee.bot/v2"
    api_key: SecretStr
    rent_duration_label: Literal["1h", "1d", "3d", "7d", "14d"] = "1h"
    energy_overprovision_factor: Decimal = Decimal("1.05")
    poll_interval_sec: float = 2.0
    timeout_sec: int = 60

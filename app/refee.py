from decimal import Decimal
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, SecretStr, field_validator


class RefeeConfig(BaseModel):
    api_base_url: str = Field(default="https://api.refee.bot/v2", min_length=1)
    api_key: SecretStr
    rent_duration_label: Literal["1h", "1d", "3d", "7d", "14d"] = "1h"
    bandwidth_rent_duration_label: Literal["1h"] = "1h"
    energy_overprovision_factor: Decimal = Field(default=Decimal("1.05"), gt=0)
    min_energy_order_amount: int = Field(default=30_000, gt=0)
    min_bandwidth_order_amount: int = Field(default=1_000, gt=0)
    poll_interval_sec: float = Field(default=2.0, gt=0)
    timeout_sec: int = Field(default=60, gt=0)

    @field_validator("api_base_url")
    @classmethod
    def validate_api_base_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("api_base_url must be an HTTPS URL")
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("api_key must not be empty")
        return value

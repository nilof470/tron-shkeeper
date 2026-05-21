from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, SecretStr, field_validator


PROFEEX_MIN_ENERGY_ORDER_AMOUNT = 64_285
PROFEEX_MAX_ENERGY_ORDER_AMOUNT = 3_000_000
PROFEEX_MIN_BANDWIDTH_ORDER_AMOUNT = 350
PROFEEX_MAX_BANDWIDTH_ORDER_AMOUNT = 10_000


class ProfeeXConfig(BaseModel):
    api_base_url: str = Field(default="https://api.profeex.io/api/v1", min_length=1)
    api_key: SecretStr
    currency: Literal["TRX", "USDT"] = "TRX"
    energy_duration_label: Literal["1h", "1d", "3d", "7d", "14d"] = "1h"
    bandwidth_duration_label: Literal["1h", "1d", "3d", "7d", "14d"] = "1h"
    fixed_energy_order_amount: int = Field(
        default=65_000,
        ge=PROFEEX_MIN_ENERGY_ORDER_AMOUNT,
        le=PROFEEX_MAX_ENERGY_ORDER_AMOUNT,
    )
    fixed_bandwidth_order_amount: int = Field(
        default=350,
        ge=PROFEEX_MIN_BANDWIDTH_ORDER_AMOUNT,
        le=PROFEEX_MAX_BANDWIDTH_ORDER_AMOUNT,
    )
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

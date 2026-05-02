import re

from functools import lru_cache
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "BranchBank25"
    app_port: int = 8081
    app_env: str = "dev"

    database_url: str = "sqlite+pysqlite:///./branchbank.db"

    user_jwt_secret: str = "change-me"
    user_jwt_ttl_minutes: int = 120

    central_bank_base_url: str = "https://test.diarainfra.com/central-bank/api/v1"
    bank_name: str = "OLL001"
    bank_public_url: str = "https://harukontor.onrender.com"
    bank_prefix: str = "OLL"
    bank_registration_id: str | None = None

    supported_currencies: str = "EUR,USD,GBP,SEK"

    keys_dir: str = "./keys"

    heartbeat_interval_seconds: int = 900
    bank_sync_interval_seconds: int = 300
    pending_retry_poll_seconds: int = 30
    pending_timeout_hours: int = 4

    @field_validator("bank_prefix")
    @classmethod
    def validate_bank_prefix(cls, value: str) -> str:
        normalized = value.strip().upper()
        if re.fullmatch(r"[A-Z0-9]{3}", normalized) is None:
            raise ValueError("BANK_PREFIX must be exactly 3 uppercase letters/digits")
        return normalized


@lru_cache
def get_settings() -> Settings:
    return Settings()

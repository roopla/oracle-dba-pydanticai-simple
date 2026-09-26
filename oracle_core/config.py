"""Application configuration."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment variables and .env."""

    oracle_host: str
    oracle_port: int = 1521
    oracle_user: str
    oracle_password: SecretStr

    oracle_domain: str | None = None
    oracle_cdb_name: str
    oracle_default_pdb_name: str

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return one cached Settings instance."""
    return Settings()
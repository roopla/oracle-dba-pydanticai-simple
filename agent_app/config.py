"""Configuration used by the PydanticAI agent application."""

from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    """Agent settings loaded from environment variables and .env."""

    openwebui_base_url: str
    openwebui_models_url: str
    openwebui_api_key: SecretStr
    llm_model: str
    mcp_server_url: str

    pydantic_ai_include_content: bool = False

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator(
        "openwebui_base_url",
        "openwebui_models_url",
        "mcp_server_url",
    )
    @classmethod
    def remove_trailing_slash(cls, value: str) -> str:
        """Remove trailing slashes from configured URLs."""
        return value.rstrip("/")


@lru_cache
def get_agent_settings() -> AgentSettings:
    """Return one cached AgentSettings instance."""
    return AgentSettings()

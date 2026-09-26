"""Create OpenWebUI-backed PydanticAI models and agents."""

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from agent_app.config import get_agent_settings


def create_model(
    model_name: str | None = None,
) -> OpenAIChatModel:
    """Create an OpenAI-compatible model connected to OpenWebUI."""
    settings = get_agent_settings()

    provider = OpenAIProvider(
        base_url=settings.openwebui_base_url,
        api_key=settings.openwebui_api_key.get_secret_value(),
    )

    selected_model = model_name or settings.llm_model

    return OpenAIChatModel(
        model_name=selected_model,
        provider=provider,
    )


def create_agent() -> Agent:
    """Create the simple agent used for the Block 4 model test."""
    return Agent(
        model=create_model(),
        instructions=(
            "You are a concise test assistant. "
            "Answer the user's question in one short sentence."
        ),
    )
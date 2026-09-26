"""Discover models available through the OpenWebUI API."""

import httpx

from pydantic_ai.models.openai import OpenAIChatModel

from agent_app.config import get_agent_settings
from agent_app.model import create_model


def fetch_available_model_ids() -> list[str]:
    """Return model IDs visible to the configured OpenWebUI API key."""
    settings = get_agent_settings()

    response = httpx.get(
        settings.openwebui_models_url,
        headers={
            "Authorization": (
                "Bearer "
                f"{settings.openwebui_api_key.get_secret_value()}"
            )
        },
        timeout=20.0,
    )

    response.raise_for_status()

    payload = response.json()
    model_entries = payload.get("data", [])

    model_ids: list[str] = []

    for model_entry in model_entries:
        if not isinstance(model_entry, dict):
            continue

        model_id = model_entry.get("id")

        if isinstance(model_id, str) and model_id.strip():
            model_ids.append(model_id.strip())

    # Remove duplicates while preserving the API order.
    return list(dict.fromkeys(model_ids))


def create_web_model_options() -> dict[str, OpenAIChatModel]:
    """Create extra model choices for the PydanticAI web UI."""
    settings = get_agent_settings()

    try:
        model_ids = fetch_available_model_ids()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        print(
            "Warning: could not retrieve OpenWebUI models. "
            f"Using only the default model. Error: {exc}"
        )
        return {}

    return {
        model_id: create_model(model_id)
        for model_id in model_ids
        if model_id != settings.llm_model
    }

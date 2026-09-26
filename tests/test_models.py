"""Test dynamic OpenWebUI model discovery."""

from agent_app.config import get_agent_settings
from agent_app.model_catalog import fetch_available_model_ids


def main() -> None:
    settings = get_agent_settings()

    print("Models endpoint:", settings.openwebui_models_url)

    model_ids = fetch_available_model_ids()

    print("\nAvailable models:")

    for model_id in model_ids:
        default_marker = (
            "  [default]"
            if model_id == settings.llm_model
            else ""
        )

        print(f"- {model_id}{default_marker}")

    assert model_ids, "OpenWebUI returned no models"

    print(f"\nTotal models: {len(model_ids)}")
    print("Dynamic model discovery passed")


if __name__ == "__main__":
    main()

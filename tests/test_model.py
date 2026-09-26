"""Block 4 test: call an OpenWebUI model through PydanticAI."""

from agent_app.config import get_agent_settings
from agent_app.model import create_agent


def main() -> None:
    settings = get_agent_settings()

    print("OpenWebUI base URL:", settings.openwebui_base_url)
    print("Model:", settings.llm_model)

    agent = create_agent()

    print("\nSending prompt to the model...")

    result = agent.run_sync(
        "Confirm that the PydanticAI model connection is working."
    )

    print("\nLLM output:")
    print(result.output)

    assert isinstance(result.output, str), (
        "The model did not return text"
    )

    assert result.output.strip(), (
        "The model returned an empty response"
    )

    print("\nBlock 4 passed")


if __name__ == "__main__":
    main()

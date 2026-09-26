"""Interactive command-line interface for the Oracle DBA agent."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

import httpx

from agent_app.agent import create_oracle_agent
from agent_app.config import get_agent_settings
from agent_app.model_catalog import fetch_available_model_ids


EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit"}


def build_model_choices(
    default_model: str,
    discovered_models: Sequence[str],
) -> list[str]:
    """Return unique model IDs with the configured default first."""
    ordered = [default_model, *discovered_models]
    return list(dict.fromkeys(model.strip() for model in ordered if model.strip()))


def resolve_model_choice(
    choice: str,
    models: Sequence[str],
    default_model: str,
) -> str:
    """Resolve an empty, numeric, or exact model selection."""
    value = choice.strip()

    if not value:
        return default_model

    if value.isdigit():
        index = int(value) - 1
        if 0 <= index < len(models):
            return models[index]
        raise ValueError(f"Choose a number from 1 to {len(models)}")

    if value in models:
        return value

    raise ValueError("Enter a listed model number or exact model ID")


def load_model_choices() -> list[str]:
    """Load OpenWebUI models, falling back to the configured default."""
    settings = get_agent_settings()

    try:
        discovered = fetch_available_model_ids()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        print(
            "Warning: could not retrieve the OpenWebUI model list. "
            f"Using the configured default only. Error: {exc}"
        )
        discovered = []

    return build_model_choices(settings.llm_model, discovered)


def print_models(models: Sequence[str], default_model: str) -> None:
    """Print numbered model choices."""
    print("\nAvailable models:\n")

    for number, model_name in enumerate(models, start=1):
        marker = " [default]" if model_name == default_model else ""
        print(f"{number}. {model_name}{marker}")


def select_model(models: Sequence[str]) -> str:
    """Prompt until the user selects a valid model."""
    settings = get_agent_settings()
    print_models(models, settings.llm_model)

    while True:
        try:
            choice = input("\nSelect model [1]: ")
            return resolve_model_choice(choice, models, settings.llm_model)
        except ValueError as exc:
            print(f"Invalid selection: {exc}")


def parse_args() -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(
        description="Oracle DBA agent command-line interface",
    )
    parser.add_argument(
        "--model",
        help="Use this OpenWebUI model without showing the selection menu.",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="List available OpenWebUI models and exit.",
    )
    return parser.parse_args()


async def chat_loop(model_name: str, models: Sequence[str]) -> None:
    """Run one in-memory CLI conversation."""
    agent = create_oracle_agent(model_name)
    message_history = []

    print("\nOracle DBA Agent")
    print(f"Model: {model_name}")
    print("Commands: /help, /model, /models, /clear, /exit")

    async with agent:
        while True:
            try:
                user_input = input("\nYou: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting Oracle DBA Agent.")
                return

            if not user_input:
                continue

            if user_input.lower() in EXIT_COMMANDS:
                print("Exiting Oracle DBA Agent.")
                return

            if user_input == "/help":
                print("\n/help   Show CLI commands")
                print("/model  Show the selected model")
                print("/models Show available models")
                print("/clear  Clear this CLI conversation history")
                print("/exit   Exit the CLI")
                continue

            if user_input == "/model":
                print(f"Current model: {model_name}")
                continue

            if user_input == "/models":
                print_models(models, get_agent_settings().llm_model)
                continue

            if user_input == "/clear":
                message_history = []
                print("Conversation history cleared.")
                continue

            try:
                if message_history:
                    result = await agent.run(
                        user_input,
                        message_history=message_history,
                    )
                else:
                    result = await agent.run(user_input)
            except Exception as exc:
                print(f"\nAgent error: {exc}")
                continue

            print(f"\nAgent: {result.output}")
            message_history = result.all_messages()


async def main() -> None:
    """Select a model and start the interactive CLI."""
    args = parse_args()
    settings = get_agent_settings()
    models = load_model_choices()

    if args.list_models:
        print_models(models, settings.llm_model)
        return

    if args.model:
        try:
            selected_model = resolve_model_choice(
                args.model,
                models,
                settings.llm_model,
            )
        except ValueError as exc:
            raise SystemExit(f"Invalid --model value: {exc}") from exc
    else:
        try:
            selected_model = select_model(models)
        except (EOFError, KeyboardInterrupt):
            print("\nNo model selected.")
            return

    await chat_loop(selected_model, models)


if __name__ == "__main__":
    asyncio.run(main())

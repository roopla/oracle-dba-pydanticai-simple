"""Small offline tests for CLI model-selection helpers."""

from agent_app.cli import build_model_choices, resolve_model_choice


def main() -> None:
    models = build_model_choices(
        "openai/default-model",
        [
            "model-b",
            "openai/default-model",
            "model-c",
            "model-b",
        ],
    )

    assert models == [
        "openai/default-model",
        "model-b",
        "model-c",
    ]
    assert resolve_model_choice("", models, models[0]) == models[0]
    assert resolve_model_choice("2", models, models[0]) == "model-b"
    assert resolve_model_choice("model-c", models, models[0]) == "model-c"

    try:
        resolve_model_choice("99", models, models[0])
    except ValueError:
        pass
    else:
        raise AssertionError("Out-of-range model selection was accepted")

    print("CLI model-selection tests passed")


if __name__ == "__main__":
    main()

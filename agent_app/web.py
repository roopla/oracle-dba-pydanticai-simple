"""Development web UI for the Oracle DBA agent."""

from agent_app.agent import create_oracle_agent
from agent_app.model_catalog import create_web_model_options


agent = create_oracle_agent()

available_models = create_web_model_options()

print("Additional selectable models:")
for model_name in available_models:
    print(f"- {model_name}")

app = agent.to_web(
    models=available_models,
)
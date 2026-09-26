"""Combined web app: existing PydanticAI chat UI + monitoring dashboard.

Your original agent_app/web.py is untouched and still works standalone.
This module wraps it:

    /            → the built-in PydanticAI chat UI (unchanged)
    /monitor     → monitoring dashboard (open it in a separate browser tab)
    /monitor/api → monitor REST API (issues, acknowledge, health)

Run it exactly like web.py, but the process also needs the Oracle env vars
(the monitor talks to Oracle directly through oracle_core):

    uv run \
      --env-file .env.otel \
      --env-file .env.mcp \
      --env-file .env.agent \
      opentelemetry-instrument \
      uvicorn agent_app.web_combined:app --host 127.0.0.1 --port 8000

(.env.mcp before .env.agent so the agent's OTEL_SERVICE_NAME wins.)

Note on lifespans: Starlette does not run mounted sub-apps' lifespans, so
the monitor's startup/shutdown are invoked here explicitly. The chat app
returned by Agent.to_web() has a default no-op lifespan, so nothing is
lost by mounting it.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from agent_app.web import app as chat_app
from monitor.api import monitor_app, shutdown as monitor_shutdown, startup as monitor_startup


@asynccontextmanager
async def lifespan(_: FastAPI):
    await monitor_startup()
    try:
        yield
    finally:
        await monitor_shutdown()


app = FastAPI(title="Oracle DBA Agent + Monitor", lifespan=lifespan)


@app.get("/monitor", include_in_schema=False)
async def monitor_redirect():
    """Allow /monitor (no trailing slash) to reach the mounted dashboard."""
    return RedirectResponse(url="/monitor/")


# Order matters: the chat app defines a greedy /{id} route,
# so it must be mounted at the root LAST.
app.mount("/monitor", monitor_app)
app.mount("/", chat_app)

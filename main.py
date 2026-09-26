"""Single web app: Chainlit chat UI + the existing monitor dashboard.

    /          -> redirects to /chat
    /chat      -> Chainlit ChatGPT-style interface (chat.py)
    /monitor/  -> your existing dashboard.html + REST API

Run from the project root:

    uv run \
      --env-file .env.otel \
      --env-file .env.mcp \
      --env-file .env.agent \
      opentelemetry-instrument \
      uvicorn main:app --host 127.0.0.1 --port 8000

Note on lifespans: Starlette does not run a mounted sub-app's lifespan, so the
monitor's startup/shutdown are invoked here explicitly - same approach you
already used in agent_app/web_combined.py.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from chainlit.utils import mount_chainlit
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from monitor.api import (
    monitor_app,
    shutdown as monitor_shutdown,
    startup as monitor_startup,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await monitor_startup()
    try:
        yield
    finally:
        await monitor_shutdown()


app = FastAPI(title="Oracle DBA Agent", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def root_redirect():
    return RedirectResponse(url="/chat")


@app.get("/monitor", include_in_schema=False)
async def monitor_redirect():
    """dashboard.html uses relative API paths, so the trailing slash matters."""
    return RedirectResponse(url="/monitor/")


app.mount("/monitor", monitor_app)

# Must come last: Chainlit takes over everything under /chat.
mount_chainlit(app=app, target="chat.py", path="/chat")

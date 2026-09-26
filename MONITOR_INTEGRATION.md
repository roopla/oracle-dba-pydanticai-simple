# Monitor Integration

Adds a continuous Oracle health monitor + dashboard to the existing DBA agent
app — **new files only, none of your existing files were modified**.

```
monitor/                      NEW — the whole monitoring subsystem
  config.py                   settings (poll interval, thresholds, sqlite path)
  models.py                   DetectedIssue / Recommendation / IssueRecord
  checks.py                   Oracle checks via your oracle_core.db.query
  llm.py                      recommendation agent via your agent_app.model.create_model
  storage.py                  SQLite (stdlib sqlite3) — issues + ack state
  poller.py                   background asyncio loop
  api.py                      FastAPI sub-app (dashboard + REST)
  static/dashboard.html       the dashboard page
agent_app/web_combined.py     NEW — mounts chat UI at /  and monitor at /monitor
```

## Install (one new dependency)

```bash
uv add fastapi
```

Everything else (starlette, oracledb, pydantic-ai, pydantic-settings) is
already in your project.

## Run

Same as before, but use `web_combined:app` and also load `.env.mcp`
(the monitor talks to Oracle directly through `oracle_core`, which needs
the ORACLE_* variables that currently only the MCP process loads):

```bash
uv run \
  --env-file .env.otel \
  --env-file .env.mcp \
  --env-file .env.agent \
  opentelemetry-instrument \
  uvicorn agent_app.web_combined:app --host 127.0.0.1 --port 8000
```

`.env.mcp` is loaded **before** `.env.agent` so the agent's
`OTEL_SERVICE_NAME` wins over the MCP one.

Then:
- Chat UI (unchanged): http://127.0.0.1:8000/
- Monitor dashboard:   http://127.0.0.1:8000/monitor  ← open in a second browser tab
- Monitor API docs:    http://127.0.0.1:8000/monitor/api/docs

Your original `agent_app.web:app` still runs standalone exactly as before.

## What it checks (every MONITOR_POLL_INTERVAL_SECONDS, default 60)

All checks run once against the CDB root using container-aware views, so
one connection point covers every PDB:

| Check | View(s) | Severity logic |
|---|---|---|
| Tablespace usage (all PDBs) | CDB_DATA_FILES, CDB_FREE_SPACE, V$CONTAINERS | WARN ≥85%, CRIT ≥95% (configurable) |
| Blocking sessions | V$SESSION | CRIT if waiting >300s |
| Long-running queries | V$SESSION | WARN ≥60s, CRIT ≥5× threshold |
| Alert log ORA- errors (last hour) | V$DIAG_ALERT_EXT | CRIT for ORA-600/ORA-7445 |
| Top wait events | V$SYSTEM_EVENT | INFO |

Deliberately **no DBA_HIST_* (AWR) views** — those need the Diagnostic Pack
license. If you're licensed, swap the wait-event query.

Each detected issue is sent to a recommendation agent built with
`create_model()` — same OpenWebUI provider and API key as your chat agent.
It uses `PromptedOutput` (JSON-by-prompt, then validated) instead of strict
tool-based structured output, because OpenWebUI-proxied models can be flaky
with strict tool schemas (you run `OPENWEBUI_STRICT_TOOLS=false`). If the
LLM call fails, the issue is still stored with a zero-confidence fallback
recommendation so nothing is silently dropped.

Issues are deduped by fingerprint; recurrences bump `occurrence_count`.
Acknowledged issues that recur are automatically re-opened.

## Optional settings (env)

```
MONITOR_POLL_INTERVAL_SECONDS=60
MONITOR_LONG_RUNNING_QUERY_SECONDS=60
MONITOR_TABLESPACE_WARN_PCT=85.0
MONITOR_TABLESPACE_CRIT_PCT=95.0
MONITOR_TOP_N_WAIT_EVENTS=5
MONITOR_SQLITE_PATH=./monitor.db
MONITOR_LLM_MODEL=            # empty = same model as the chat agent
```

## Design decisions you may want to revisit

- **Poller lives in the web process.** Simple, but if you scale uvicorn to
  multiple workers, each worker runs its own poller. Keep `--workers 1`
  (the default) or split the poller into its own process later.
- **`oracle_core.db.query` opens a connection per query.** Fine at a 60s
  interval; if you shorten it aggressively, consider adding a pool to
  oracle_core (that would be the one change to existing code worth making).
- **LLM cost/noise:** wait events fire every cycle at INFO severity, but
  dedup means the LLM is only called when the poller processes an issue —
  currently every cycle per issue. If gpt-oss-120b is slow/expensive for
  you, set `MONITOR_LLM_MODEL` to a smaller model, or we can add
  "skip re-analysis of unchanged issues" as a next step.
- **OTel:** the poller's Oracle and LLM calls will be traced by your
  existing auto-instrumentation (dbapi/httpx), so cycles show up in Jaeger.

## Security notes

- The `/monitor` routes have **no authentication**, same as the chat UI —
  fine on 127.0.0.1, add auth before exposing beyond localhost.
- The zip you shared contained a live `OPENWEBUI_API_KEY` and Oracle
  password — **rotate that key**.

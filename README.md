# Oracle DBA Agent

A chat assistant for Oracle 19c DBAs and developers. It answers questions
about a live database through read-only tools, watches the database in the
background and explains the incidents it finds, and can fix a small set of
known problems, but only after a human approves each change.

- **Chat** (`/chat`): ask about PDBs, sessions, blocking, top SQL, waits,
  ASH history, tablespaces or Data Guard. Answers come from live queries,
  never from memory.
- **Monitor** (`/monitor/`): six checks every 15 s, incidents with an
  ACTIVE / RESOLVED lifecycle, and an LLM recommendation per new incident.
  The Monitor link in the chat header turns red while a critical incident is
  unacknowledged.
- **Approved fixes**: restart redo apply or the standby instance, enable
  tablespace autoextend, or add a datafile. The agent can only propose them;
  each runs after you click Approve on its card, and every decision is
  audited.

See [ARCHITECTURE.md](ARCHITECTURE.md) for diagrams of the components and
the main flows.

## Stack

| Part | Technology |
| --- | --- |
| Agent | [PydanticAI](https://ai.pydantic.dev/) with any OpenAI-compatible model endpoint |
| Tools | [FastMCP](https://gofastmcp.com/) server over HTTP |
| Oracle access | python-oracledb (thin mode) |
| Web | Chainlit chat UI + FastAPI monitor, one uvicorn app (`main.py`) |
| Monitor state | SQLite (`monitor.db`) |
| Tracing | OpenTelemetry to Jaeger |

## Requirements

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- An Oracle 19c database with a PDB; a Data Guard physical standby is
  optional (Data Guard questions and fixes need it)
- An OpenAI-compatible endpoint such as OpenWebUI, with an API key
- Optional: Jaeger with an OTLP HTTP receiver, for traces

## Setup

1. Install dependencies:

   ```bash
   uv sync
   ```

2. Copy the environment templates and fill them in. The real files are
   git-ignored.

   | File | Holds |
   | --- | --- |
   | `.env.mcp` | Oracle host, port, credentials, CDB and default PDB; standby DSN and SYSDBA login; SSH settings for standby restarts |
   | `.env.agent` | OpenWebUI URLs, API key and model; MCP server URL; monitor thresholds |
   | `.env.otel` | OTLP endpoint for tracing |
   | `.env.loadgen` | Credentials for the ingest simulator's `LOADGEN` schema |

   ```bash
   cp .env.mcp.example .env.mcp
   cp .env.agent.example .env.agent
   cp .env.otel.example .env.otel
   ```

## Run

Start the MCP server and the web app, each in its own terminal, from the
project root:

```bash
uv run --env-file .env.otel --env-file .env.mcp \
  opentelemetry-instrument python -m mcp_server.server

uv run --env-file .env.otel --env-file .env.mcp --env-file .env.agent \
  opentelemetry-instrument uvicorn main:app --host 127.0.0.1 --port 8000
```

Without Jaeger, drop `--env-file .env.otel` and `opentelemetry-instrument`.

| URL | What |
| --- | --- |
| http://127.0.0.1:8000/chat | Chat |
| http://127.0.0.1:8000/monitor/ | Monitor dashboard |
| http://127.0.0.1:8000/monitor/api/health | Monitor health check |

For a terminal chat instead of the web UI:

```bash
uv run --env-file .env.agent python -m agent_app.cli
```

## Tests

```bash
uv run --with pytest --with pytest-asyncio \
  --env-file .env.mcp --env-file .env.agent \
  pytest tests --ignore=tests/test_pdb_validation.py --ignore=tests/test_queries.py
```

The two ignored files query a live database and expect a PDB named
`ORCLPDB2`.

## More documentation

| Document | Covers |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, request and approval flow, monitor cycle, allowlisted changes |
| [docker-stuff/README.md](docker-stuff/README.md) | Building the Data Guard lab: primary from a seed image, standby by RMAN duplicate (scripts `00`–`03`) |
| [RUNBOOK.md](RUNBOOK.md) | Stopping, starting and verifying the stack. Written for an earlier lab setup, so its database and container names may not match yours. |
| [INGEST_SIMULATOR.md](INGEST_SIMULATOR.md) | The demo load generator: modes, commands, recipes |
| [SHARED_QUERY_LAYER.md](SHARED_QUERY_LAYER.md) | Why all SQL lives in `oracle_core/` |
| [MONITOR_INTEGRATION.md](MONITOR_INTEGRATION.md) | How the monitor is wired into the app |
| [MONITOR_APPROVED_SQL.md](MONITOR_APPROVED_SQL.md), [MONITOR_BAKED_DIAGNOSTICS.md](MONITOR_BAKED_DIAGNOSTICS.md) | The reviewed SQL the monitor shows and runs |

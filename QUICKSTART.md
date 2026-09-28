# Quick start: new environment to tested demo

The shortest path from a fresh clone to a working agent with every demo
scenario tested, in a **lab or test** environment. The only files you edit
are the four `.env` files. For production rollouts, security decisions and
every setting, use [DEPLOYMENT.md](DEPLOYMENT.md); for more detail on each
scenario, see [SCENARIOS.md](SCENARIOS.md).

About 30 minutes for steps 1 to 6, then 3 to 10 minutes per scenario.

## 0. What you need

| Need | Notes |
| --- | --- |
| Python 3.13 and [uv](https://docs.astral.sh/uv/) | No Oracle client: python-oracledb runs in thin mode |
| Oracle 19c or 21c CDB with a PDB | A **lab or test** database: the scenarios degrade it on purpose |
| A DBA login to that database | `SYSTEM` or similar, for the one-time setup |
| An OpenAI-compatible LLM endpoint and API key | The model must support tool calling |
| Optional: a physical standby | Only for the two Data Guard scenarios; its SYSDBA password |
| Optional: an OTLP collector such as Jaeger | Only for tracing |
| Optional: the Oracle Diagnostics Pack licence | Only for the history (ASH) scenario |

## 1. Clone and install

```bash
git clone https://github.com/<org>/oracle-dba-pydanticai-simple.git
cd oracle-dba-pydanticai-simple
uv sync

cp .env.mcp.example     .env.mcp
cp .env.agent.example   .env.agent
cp .env.loadgen.example .env.loadgen
cp .env.otel.example    .env.otel      # only if you use tracing
```

## 2. Create the agent's database account

In the CDB root, as a DBA. The first block is the read-only account; the
second lets the tablespace and partition fixes run when you approve them
in the scenarios.

```sql
CREATE USER C##DBA_AGENT IDENTIFIED BY "<password>" CONTAINER=ALL;
GRANT CREATE SESSION        TO C##DBA_AGENT CONTAINER=ALL;
GRANT SELECT ANY DICTIONARY TO C##DBA_AGENT CONTAINER=ALL;
ALTER USER C##DBA_AGENT SET CONTAINER_DATA=ALL CONTAINER=CURRENT;

-- For the fixes the scenarios test (skip for a read-only agent)
GRANT ALTER DATABASE, ALTER TABLESPACE TO C##DBA_AGENT CONTAINER=ALL;
GRANT ALTER ANY TABLE, DROP ANY TABLE  TO C##DBA_AGENT CONTAINER=ALL;
```

Find the service names the agent will use:

```bash
lsnrctl services      # on the database host
```

## 3. Fill in the `.env` files

**`.env.mcp`** (database)

| Setting | Value |
| --- | --- |
| `ORACLE_HOST`, `ORACLE_PORT` | Primary listener |
| `ORACLE_USER`, `ORACLE_PASSWORD` | `C##DBA_AGENT` and its password |
| `ORACLE_CDB_NAME` | CDB root service name, without domain |
| `ORACLE_DOMAIN` | Domain part of the service names, or empty |
| `ORACLE_DEFAULT_PDB_NAME` | The PDB the scenarios use |
| `ORACLE_STANDBY_DSN`, `ORACLE_STANDBY_PASSWORD` | `host:port/service` of the mounted standby and its SYS password; empty without a standby |
| `STANDBY_SSH_*`, `STANDBY_CONTAINER` | Leave empty unless the standby runs in Docker |

**`.env.agent`** (LLM and monitor): `OPENWEBUI_BASE_URL`,
`OPENWEBUI_MODELS_URL`, `OPENWEBUI_API_KEY` and `LLM_MODEL`. The names
are historical; any OpenAI-compatible endpoint works. Keep the default
monitor thresholds for now.

**`.env.loadgen`** (scenarios)

| Setting | Value |
| --- | --- |
| `LAB_SCENARIOS_ENABLED` | `true` (lab or test database only) |
| `LOADGEN_ORACLE_PASSWORD` | Any password; `prepare` creates the `LOADGEN` user with it |
| `SCENARIO_ADMIN_USER`, `SCENARIO_ADMIN_PASSWORD` | The DBA login from section 0 |

**`.env.otel`** (tracing only): `OTEL_EXPORTER_OTLP_ENDPOINT`.

## 4. Run the unit tests

They check the install and do not touch the database.

```bash
uv run --with pytest --with pytest-asyncio \
  --env-file .env.mcp --env-file .env.agent \
  pytest tests --ignore=tests/test_pdb_validation.py --ignore=tests/test_queries.py
```

Expected: all pass.

## 5. Start the agent

Two terminals, both from the repository root. Start the MCP server first.

```bash
# Terminal 1: MCP server (port 9000)
uv run --env-file .env.mcp python -m mcp_server.server

# Terminal 2: web app (port 8000)
uv run --env-file .env.mcp --env-file .env.agent \
  uvicorn main:app --host 127.0.0.1 --port 8000
```

With tracing, add `--env-file .env.otel` first and put
`opentelemetry-instrument` before `python` / `uvicorn`.

Open the chat at http://127.0.0.1:8000/chat and the monitor at
http://127.0.0.1:8000/monitor/.

## 6. Smoke test

| # | Do | Expect |
| --- | --- | --- |
| 1 | `curl -s http://127.0.0.1:8000/monitor/api/health` | `{"status":"ok","poller_running":true}` |
| 2 | Chat: *"List the PDBs and their open modes"* | Your PDBs |
| 3 | Chat: *"Show tablespace usage for <PDB>"* | Real numbers for that PDB |
| 4 | Chat: *"Is my Data Guard standby healthy?"* (with a standby) | Details from both primary and standby |
| 5 | Chat: *"What remediation actions are available?"* | The action list. Without SSH settings, `restart_standby_instance` is marked unavailable |

If a step fails, see [DEPLOYMENT.md section 12](DEPLOYMENT.md#12-troubleshooting).

## 7. Prepare the scenarios

In a third terminal, from the repository root, define a short command:

```bash
# bash
scen() { uv run --env-file .env.mcp --env-file .env.agent --env-file .env.loadgen \
             python scripts/scenarios.py "$@"; }
```

```powershell
# PowerShell
function scen { uv run --env-file .env.mcp --env-file .env.agent --env-file .env.loadgen python scripts/scenarios.py @args }
```

Then create the load schema and base tables, and check:

```bash
scen prepare
scen status
```

## 8. Test each scenario

For each row: run the command, wait for the monitor (one poll interval,
15 s by default), open a **new chat**, ask the question and compare. Run
them one at a time.

| # | Command | Monitor | Ask | Pass when |
| --- | --- | --- | --- | --- |
| 1 | `scen write-load --level warning` | `WRITE_WORKLOAD` WARNING | *"Is there any unusual write activity right now?"* | The agent names the `ingest_simulator` sessions and their commit rate |
| 2 | `scen write-load --level critical` | `WRITE_WORKLOAD` CRITICAL; Monitor link turns red | *"What is generating load right now?"* | The `INSERT INTO INGEST_SIM_ORDERS` statement. Acknowledge the incident on the dashboard: the link stops being red |
| 3 | `scen blocking --seconds 120` | `BLOCKING_SESSION` | *"Is anything blocked right now?"* | Blocker and waiter SIDs match the dashboard; wait is `enq: TX - row lock contention` |
| 4 | `scen long-query --seconds 150` | `LONG_RUNNING_QUERY` | *"Are there any long-running sessions?"* | The session, its sql_id and elapsed time |
| 5 | `scen tablespace-full` | `TABLESPACE_USAGE` CRITICAL for `SCEN_FULL_TS` | *"SCEN_FULL_TS in <PDB> is full, how can we fix it?"* | Two option cards: autoextend or add a datafile. Approve one; the other is withdrawn, and the incident resolves on the next poll |
| 6 | `scen partition-pressure` | `TABLESPACE_USAGE` CRITICAL for `PART_DEMO_TS` | *"PART_DEMO_TS in <PDB> is almost full, what can we do?"* | A third card: drop old partitions of `SALES_HISTORY`, marked as data deletion. Approve it: the current and previous month remain; usage drops from about 97% to under 40% |
| 7 | `scen dg-stop-apply` (standby only) | | *"Is my Data Guard standby healthy?"*, then *"Can you fix it?"* | "No": MRP0 missing and apply lag growing. Approve `restart_redo_apply`; `scen status` then shows MRP0 back and the gap closing |
| 8 | `scen history-burst --minutes 3` (Diagnostics Pack only), then ask right after it ends | | *"What was the database waiting on in the last 8 minutes?"*, then *"Which SQL was responsible?"* | Commit waits (`log file sync`), then only the `INSERT INTO INGEST_SIM_ORDERS` sql_id |

The load scenarios stop by themselves; `Ctrl+C` stops them earlier. If
scenario 7 is left unfixed, `scen dg-start-apply` restarts redo apply.

Optional: `scen write-load --level burst --duration 600` shows an
incident opening, resolving and reopening with each burst.

## 9. Reset

```bash
scen cleanup-all --yes              # drop the scenario tables and tablespaces
scen cleanup-all --yes --drop-user  # also drop the LOADGEN schema
```

Run `scen prepare` to start again. Nothing else in the database is
touched.

## Common problems

| Symptom | Fix |
| --- | --- |
| A scenario says it refuses to run | Set `LAB_SCENARIOS_ENABLED=true` in `.env.loadgen` |
| `ORA-01031` during `prepare` or a scenario | `SCENARIO_ADMIN_*` must be a DBA login |
| `ORA-01031` when approving a fix | The fix grants from step 2 are missing |
| `ORA-12514` | Service names: check `ORACLE_CDB_NAME` and `ORACLE_DOMAIN` against `lsnrctl services` |
| No incident appears | Wait one poll interval; check the web app terminal for check errors |
| Scenario 1 reaches CRITICAL or stays below WARNING | Tune the `MONITOR_WRITE_*` thresholds in `.env.agent` and restart the web app |
| Scenario 5 or 6 stops on ASM | Create the tablespace by hand; see [SCENARIOS.md](SCENARIOS.md#4-notes) |

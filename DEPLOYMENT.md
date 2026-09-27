# Deploying the Oracle DBA Agent

How to install, configure, secure and verify the agent in a new
environment. For what the parts are and how they talk to each other, see
[ARCHITECTURE.md](ARCHITECTURE.md).

**Read [Before you start](#1-before-you-start) first.** Three things in it
decide whether this is safe to run where you are: the chat UI has no login,
one tool needs an Oracle licence option, and some fixes change the database.

## Contents

1. [Before you start](#1-before-you-start)
2. [What you deploy](#2-what-you-deploy)
3. [Prerequisites](#3-prerequisites)
4. [Oracle setup](#4-oracle-setup)
5. [Install the application](#5-install-the-application)
6. [Configuration reference](#6-configuration-reference)
7. [Run it](#7-run-it)
8. [Verify the deployment](#8-verify-the-deployment)
9. [Security checklist](#9-security-checklist)
10. [Remediation actions: what to enable](#10-remediation-actions-what-to-enable)
11. [Operations](#11-operations)
12. [Troubleshooting](#12-troubleshooting)

## 1. Before you start

| Decision | Why it matters | What to do |
| --- | --- | --- |
| **Who can reach the web UI** | The chat UI has **no authentication**. Anyone who can open it can ask about the database and **approve changes**. Every approval is audited as the fixed name `chat-ui`, not a person. | Keep it on `127.0.0.1` and put an authenticating reverse proxy (SSO) in front, or limit access with a firewall to named DBA hosts. Do not expose it on a shared network as is. |
| **Diagnostics Pack licence** | The `get_ash_activity` tool reads `V$ACTIVE_SESSION_HISTORY`, which is licensed under the **Oracle Diagnostics Pack**. The agent calls it for questions about past time windows ("what happened at 2pm?"). Everything else uses free `V$`, `DBA_` and `CDB_` views; nothing uses AWR (`DBA_HIST_*`). | If the target databases are not licensed, remove the tool before deploying: in `mcp_server/server.py`, delete the `@mcp.tool` line above `def get_ash_activity`. There is no configuration switch for this. |
| **Read-only, or with fixes** | The agent can propose five changes that run after a human clicks Approve (section 10). Some are irreversible, one deletes data. | For a first rollout, deploy **read-only**: give the database account only the read grants (section 4.1). An approved database fix then fails with `ORA-01031`, shown in its result. The two **standby** fixes use the standby's SYSDBA login instead, so they stay possible while `ORACLE_STANDBY_*` is set; remove those actions (section 10) if even they must not run. |
| **Where the LLM runs** | Prompts include query results: SQL text, session details, object names, error messages. They go to whatever endpoint you configure. | Use an endpoint approved for this data (internal gateway or a provider under your data agreement). |

## 2. What you deploy

| Process | Command | Port | Talks to |
| --- | --- | --- | --- |
| **MCP server** | `python -m mcp_server.server` | 9000 (loopback) | Oracle (read-only queries and plans) |
| **Web app** | `uvicorn main:app` | 8000 | MCP server, LLM endpoint, Oracle (monitor checks and approved fixes) |

Both are Python processes from the same repository and read the same
`.env.mcp`. Optional pieces: an OTLP trace collector such as Jaeger, and
the lab simulators in `scripts/` (never run those against production).

State is one SQLite file, `monitor.db`, holding monitor incidents and the
audit trail of approvals and rejections.

## 3. Prerequisites

**Application host**

- Linux or Windows; Linux is simpler to run as a service.
- Python **3.13** and [uv](https://docs.astral.sh/uv/). No Oracle client is
  needed: python-oracledb runs in thin mode.
- Git access to the repository.

**Network**

| From | To | Port | Needed for |
| --- | --- | --- | --- |
| Application host | Primary database listener | 1521 (or yours) | Everything |
| Application host | Standby database listener | yours | Data Guard status and standby fixes (optional) |
| Application host | LLM endpoint | 443 / yours | Chat and monitor recommendations |
| Application host | OTLP collector | 4318 | Tracing (optional) |
| DBA browsers | Application host (via your proxy) | 8000 or the proxy's | The UI |

**LLM endpoint**

- OpenAI-compatible Chat Completions API (OpenWebUI, LiteLLM, vLLM, Azure
  OpenAI through a compatible gateway, and so on), with an API key.
- The model must support **tool calling**; the agent works only through
  tools. It also needs a models list endpoint (`/models`) for the model
  picker.
- If the endpoint uses a corporate CA, point `SSL_CERT_FILE` at the CA
  bundle (section 6).

**Oracle**

- Oracle 19c or 21c, multitenant (CDB with PDBs). The code connects to the
  CDB root service and to each PDB's service.
- Optional: a physical standby for the Data Guard features.

## 4. Oracle setup

### 4.1 The agent's database account (read-only)

Create a **common user** in the CDB root. These are the exact grants the
application was tested with; every read-only tool and all six monitor
checks work with them.

```sql
-- Connected to CDB$ROOT as a DBA
CREATE USER C##DBA_AGENT IDENTIFIED BY "<strong password>" CONTAINER=ALL;
GRANT CREATE SESSION        TO C##DBA_AGENT CONTAINER=ALL;
GRANT SELECT ANY DICTIONARY TO C##DBA_AGENT CONTAINER=ALL;
-- Let CDB_* and V$ views in the root show every PDB, not just the root.
ALTER USER C##DBA_AGENT SET CONTAINER_DATA=ALL CONTAINER=CURRENT;
```

Use a profile that suits a service account (password expiry, failed
logins), and store the password in your secret store.

### 4.2 Extra grants for the fixes (optional)

Add only what you want to allow. Each grant was tested with its action.

| Action | Grants | Scope of the grant |
| --- | --- | --- |
| `enable_tablespace_autoextend` | `ALTER DATABASE` | Changes datafile autoextend; also allows much more (for example, other `ALTER DATABASE` operations) |
| `add_tablespace_datafile` | `ALTER TABLESPACE` | Any tablespace |
| `drop_old_partitions` | `ALTER ANY TABLE`, `DROP ANY TABLE` | Any table in any application schema. Powerful: prefer running the account only where monthly-partitioned tables are expected |

```sql
GRANT ALTER DATABASE, ALTER TABLESPACE TO C##DBA_AGENT CONTAINER=ALL;
GRANT ALTER ANY TABLE, DROP ANY TABLE  TO C##DBA_AGENT CONTAINER=ALL;
```

These are broad system privileges. The application limits what it will run
(allowlisted statements, validated parameters, human approval), but the
account itself could do more if its password leaked. Weigh that before
granting.

### 4.3 Standby access (optional)

Data Guard status reads the standby directly, and the standby fixes run
there. A **mounted** standby accepts only administrative connections, so:

- The standby needs `REMOTE_LOGIN_PASSWORDFILE=EXCLUSIVE` and a password
  file containing the user in `ORACLE_STANDBY_USER` (normally `SYS`, copied
  from the primary).
- Its listener must route the service in `ORACLE_STANDBY_DSN` to the
  instance while it is mounted (a static `SID_LIST` entry is the reliable
  way).
- The code connects **as SYSDBA**. Connecting with the narrower `SYSDG`
  privilege is not supported yet.

Without a standby, leave `ORACLE_STANDBY_*` empty: Data Guard answers then
say the standby could not be read, and the standby fixes refuse to plan.

### 4.4 Service names

The application builds each address as `ORACLE_HOST:ORACLE_PORT/<service>`:

- the CDB root service = `ORACLE_CDB_NAME`, plus `.ORACLE_DOMAIN` if set;
- each PDB's service = the PDB name, plus `.ORACLE_DOMAIN` if set.

Check them with `lsnrctl services` on the database host. If your PDB
services are named differently from the PDBs, this layout will not fit
without a code change (`oracle_core/db.py`, `build_service_name`). Only
plain TCP is supported; TCPS (TLS to the listener) needs a code change.

## 5. Install the application

```bash
git clone https://github.com/<org>/oracle-dba-pydanticai-simple.git
cd oracle-dba-pydanticai-simple
uv sync                        # creates .venv with the locked dependencies
cp .env.mcp.example   .env.mcp
cp .env.agent.example .env.agent
cp .env.otel.example  .env.otel    # only if you use tracing
chmod 600 .env.mcp .env.agent       # they hold passwords and keys
```

Run it under a dedicated service account, not a personal login.

## 6. Configuration reference

The processes read settings from environment variables, normally supplied
with `uv run --env-file`. Values below are examples.

### `.env.mcp` — used by both processes

| Setting | Required | Example | Meaning |
| --- | --- | --- | --- |
| `ORACLE_HOST` | Yes | `dbhost.example.com` | Primary listener host |
| `ORACLE_PORT` | No | `1521` | Listener port (default 1521) |
| `ORACLE_USER` | Yes | `C##DBA_AGENT` | Account from section 4 |
| `ORACLE_PASSWORD` | Yes | | Its password |
| `ORACLE_CDB_NAME` | Yes | `PRODCDB` | CDB root service name (without domain) |
| `ORACLE_DOMAIN` | No | `example.com` | Appended to service names; leave empty if your services have no domain |
| `ORACLE_DEFAULT_PDB_NAME` | Yes | `SALESPDB` | PDB used when a question does not name one |
| `ORACLE_STANDBY_DSN` | No | `stbyhost:1521/PRODCDB_STBY` | Full address of the standby service |
| `ORACLE_STANDBY_USER` | No | `sys` | SYSDBA user on the standby (default `sys`) |
| `ORACLE_STANDBY_PASSWORD` | No | | Its password |
| `STANDBY_SSH_HOST`, `_PORT`, `_USER`, `_PASSWORD` or `_KEY_FILE`, `STANDBY_CONTAINER` | No | | Only for `restart_standby_instance`; see section 10 |
| `MCP_HOST` | No | `127.0.0.1` | MCP server bind address. Keep loopback: the server has no authentication |
| `MCP_PORT` | No | `9000` | MCP server port |
| `MCP_PATH` | No | `/mcp` | MCP endpoint path |
| `OTEL_SERVICE_NAME` | No | `oracle-dba-mcp` | Service name in traces |

### `.env.agent` — web app

| Setting | Required | Example | Meaning |
| --- | --- | --- | --- |
| `OPENWEBUI_BASE_URL` | Yes | `https://llm-gateway.example.com/api` | Base URL of the OpenAI-compatible API (the name is historical; any compatible endpoint works) |
| `OPENWEBUI_MODELS_URL` | Yes | `https://llm-gateway.example.com/api/models` | Models list, for the picker |
| `OPENWEBUI_API_KEY` | Yes | | API key |
| `LLM_MODEL` | Yes | | Default model ID; must support tool calling |
| `MCP_SERVER_URL` | Yes | `http://127.0.0.1:9000/mcp` | Where the agent finds the MCP server |
| `MONITOR_LLM_MODEL` | No | | A cheaper model for monitor recommendations; empty = `LLM_MODEL` |
| `MONITOR_POLL_INTERVAL_SECONDS` | No | `60` | Seconds between monitor polls (default 60) |
| `MONITOR_TABLESPACE_WARN_PCT` / `_CRIT_PCT` | No | `85` / `95` | Tablespace thresholds, measured against maximum size (autoextend included) |
| `MONITOR_LONG_RUNNING_QUERY_SECONDS` | No | `60` | Long-running query threshold |
| `MONITOR_TOP_N_WAIT_EVENTS` | No | `5` | Wait events reported per poll |
| `MONITOR_WRITE_WARN_COMMITS_PER_SEC` / `_CRIT_` | No | `40` / `150` | Write-workload thresholds; also `_REDO_MB_PER_SEC` and `_EXECUTES_PER_SEC`. Tune to each database's normal load |
| `MONITOR_SQLITE_PATH` | No | `/var/lib/dba-agent/monitor.db` | Incident and audit database (default `./monitor.db`) |
| `SSL_CERT_FILE` | No | `/etc/pki/tls/certs/corp-ca.pem` | CA bundle for HTTPS to the LLM endpoint |
| `OTEL_SERVICE_NAME` | No | `oracle-dba-agent` | Service name in traces |

### `.env.otel` — tracing (optional)

| Setting | Example | Meaning |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4318` | Collector address |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | |
| `OTEL_TRACES_EXPORTER` | `otlp` | |
| `PYDANTIC_AI_INCLUDE_CONTENT` | `false` | `true` puts prompts, tool results and answers into traces. Keep `false` unless the trace store is approved for that data |

### Settings that do nothing

The example files contain a few settings the code does not read:
`LLM_PROVIDER`, `OPENWEBUI_STRICT_TOOLS`, `AGENT_API_HOST`,
`AGENT_API_PORT`, `AUDIT_LOG_DIR`, `AUDIT_LOG_FINAL_ANSWER`,
`MCP_TRANSPORT`, `DB_DEBUG`. Changing them has no effect.

## 7. Run it

Start the MCP server first, then the web app, each from the repository
root.

```bash
# MCP server
uv run --env-file .env.mcp python -m mcp_server.server

# Web app
uv run --env-file .env.mcp --env-file .env.agent \
  uvicorn main:app --host 127.0.0.1 --port 8000
```

With tracing, add `--env-file .env.otel` before the others and put
`opentelemetry-instrument` before `python` / `uvicorn`.

### As services (Linux, systemd)

`/etc/systemd/system/dba-agent-mcp.service`:

```ini
[Unit]
Description=Oracle DBA agent - MCP server
After=network-online.target

[Service]
User=dbaagent
WorkingDirectory=/opt/oracle-dba-agent
ExecStart=/home/dbaagent/.local/bin/uv run --env-file .env.mcp python -m mcp_server.server
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/dba-agent-web.service`:

```ini
[Unit]
Description=Oracle DBA agent - web app
After=dba-agent-mcp.service
Requires=dba-agent-mcp.service

[Service]
User=dbaagent
WorkingDirectory=/opt/oracle-dba-agent
ExecStart=/home/dbaagent/.local/bin/uv run --env-file .env.mcp --env-file .env.agent uvicorn main:app --host 127.0.0.1 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Use the path `which uv` prints for the service account in `ExecStart`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dba-agent-mcp dba-agent-web
journalctl -u dba-agent-web -f
```

On Windows, run the same two commands as services with a service wrapper
such as NSSM, or as scheduled tasks at startup, under a service account.

## 8. Verify the deployment

Work down the list; each step depends on the ones before it.

| # | Check | Expected |
| --- | --- | --- |
| 1 | `curl -s http://127.0.0.1:8000/monitor/api/health` | `{"status":"ok","poller_running":true}` |
| 2 | MCP tools list (below) | 16 tools, or 15 if you removed `get_ash_activity` |
| 3 | In the chat: *"List the PDBs and their open modes"* | Your PDBs, from a `list_pdbs` tool step |
| 4 | *"Show tablespace usage for <PDB>"* | Numbers that match `DBA_DATA_FILES` / `DBA_FREE_SPACE` |
| 5 | *"Is my Data Guard standby healthy?"* (if configured) | Role, lag, MRP0 and gaps from **both** sides; no "standby could not be read" note |
| 6 | Open `/monitor/` after two poll intervals | Incidents appear or the list is empty; no check errors in the logs |
| 7 | Ask the agent what it can fix: *"What remediation actions are available?"* | The action list; do not approve anything yet |

MCP tools list:

```bash
uv run python -c "
import asyncio
from fastmcp import Client
async def main():
    async with Client('http://127.0.0.1:9000/mcp') as c:
        print(len(await c.list_tools()), 'tools')
asyncio.run(main())"
```

Run the unit tests once on the new host (they do not touch the database):

```bash
uv run --with pytest --with pytest-asyncio \
  --env-file .env.mcp --env-file .env.agent \
  pytest tests --ignore=tests/test_pdb_validation.py --ignore=tests/test_queries.py
```

## 9. Security checklist

- [ ] Web UI reachable only through an authenticating proxy or from named
      DBA hosts (section 1). Consider every UI user able to approve changes.
- [ ] MCP server bound to `127.0.0.1` (`MCP_HOST`): it has no
      authentication and its tools read the whole database.
- [ ] Database account is `C##DBA_AGENT` with the section 4 grants, not
      `SYSTEM`; remediation grants only for the actions you enable.
- [ ] `.env.mcp` and `.env.agent` readable only by the service account;
      passwords and API keys from your secret store.
- [ ] LLM endpoint approved for database metadata and query results.
- [ ] `PYDANTIC_AI_INCLUDE_CONTENT=false` unless the trace store is
      approved for that content.
- [ ] `get_ash_activity` removed if the Diagnostics Pack is not licensed.
- [ ] `monitor.db` backed up: it holds the approval and rejection audit
      trail (`remediation_audit` table).
- [ ] Lab scripts in `scripts/` and `docker-stuff/` not run against
      production. The simulators are designed to degrade a database.

## 10. Remediation actions: what to enable

Every action runs only after a human approves its card. Statements come
from templates in `oracle_core/remediation.py`; the model only picks the
action and parameters, which are validated.

| Action | Changes | Reversible | Works in any environment? |
| --- | --- | --- | --- |
| `restart_redo_apply` | Restarts redo apply (MRP0) on the standby | Yes | Yes, with standby access (4.3) |
| `restart_standby_instance` | `SHUTDOWN ABORT` + `STARTUP MOUNT` of the standby | No | **No.** It runs SQL*Plus through `docker exec` on the standby host, so it only works where the standby is a Docker container. Leave `STANDBY_SSH_*` empty elsewhere: the action then refuses to plan, and the agent does not offer it |
| `enable_tablespace_autoextend` | Autoextend on / higher MAXSIZE for a tablespace's datafiles | Yes | Yes, with the grant (4.2) |
| `add_tablespace_datafile` | New datafile next to the existing ones | No | Yes. Assumes the directory of the existing files; not suited to ASM or OMF-only layouts without a code change |
| `drop_old_partitions` | Drops monthly partitions older than the retention window | No, **deletes data** | Yes, with the grants (4.2). Applies to any application table range-partitioned by month; always keeps the current and previous month |

To remove an action entirely, delete its entry from the `ACTIONS` registry
at the end of `oracle_core/remediation.py`; there is no configuration
switch. Details of the partition action: [PARTITION_PRESSURE_DEMO.md](PARTITION_PRESSURE_DEMO.md).

## 11. Operations

- **Logs:** both processes log to stdout (journald under systemd).
  Failed monitor checks are logged per check and do not stop the others.
- **Monitor database:** `monitor.db` grows slowly (one row per incident
  plus one per approval). Back it up with the application. To start the
  incident history afresh, stop the web app and use
  `scripts/reset_monitor_db.sh` (it keeps a backup copy).
- **Thresholds:** after a week, compare incidents with what the DBAs
  consider normal and tune the `MONITOR_*` thresholds per environment.
- **LLM cost:** the chat calls the model per question; the monitor calls
  it once per new, reopened or re-graded incident, not every poll.
- **Upgrades:** `git pull`, `uv sync`, restart both services, then repeat
  checks 1 to 3 of section 8.

## 12. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `ORA-12514` / `DPY-6005` on start | Service name mismatch | Check `ORACLE_CDB_NAME`, `ORACLE_DOMAIN` against `lsnrctl services` (4.4) |
| `ORA-01017` | Wrong password, or the account is not a common user in the PDB | Recreate as `C##...` with `CONTAINER=ALL` |
| Tablespace or session answers show only the root | `CONTAINER_DATA` not set | `ALTER USER C##DBA_AGENT SET CONTAINER_DATA=ALL CONTAINER=CURRENT` in the root |
| `ORA-01031` when approving a fix | Remediation grant missing | Add it (4.2), or leave the deployment read-only on purpose |
| Data Guard answers say the standby could not be read | Standby DSN, password file or listener | Section 4.3; test with `sqlplus sys@"<ORACLE_STANDBY_DSN>" as sysdba` |
| Chat shows "Error: ... Connection refused" | MCP server not running or wrong `MCP_SERVER_URL` | Start it; check section 8 step 2 |
| Model list is empty, or the agent answers without tool steps | Endpoint or model | Check `OPENWEBUI_MODELS_URL`; use a model with tool calling |
| Certificate errors to the LLM endpoint | Corporate CA | Set `SSL_CERT_FILE` |
| Monitor link colour does not change after an upgrade | Browser cached the old script | `Ctrl+Shift+R` |

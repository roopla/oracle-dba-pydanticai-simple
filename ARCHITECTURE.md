# Architecture

An Oracle DBA assistant: a chat agent that answers questions about a live
Oracle database, optionally with a Data Guard standby, using read-only tools, a background monitor that
detects incidents and explains them, and a human-approval path for the few
changes it is allowed to make.

## Components

```mermaid
flowchart TB
    user(["DBA / developer<br/>browser"])

    subgraph web["Web app process · uvicorn main:app :8000"]
        direction TB
        subgraph chatside["Chat"]
            direction LR
            chat["Chainlit chat UI<br/>/chat"]
            agent["PydanticAI agent<br/>agent_app/agent.py"]
            cards["Approval cards<br/>chat_actions.py"]
        end
        subgraph monitor["Monitor"]
            direction LR
            dash["Dashboard + API<br/>/monitor"]
            poller["Poller · every 15 s<br/>6 checks + LLM recommendation"]
            sqlite[("monitor.db<br/>incidents + audit")]
        end
    end

    subgraph mcpproc["MCP server process · FastMCP :9000"]
        mcp["MCP tools<br/>read-only + propose"]
    end

    llm["LLM<br/>OpenAI-compatible API"]
    core["oracle_core library<br/>loaded by both processes"]

    subgraph lab["Oracle Data Guard"]
        direction LR
        pri[("Primary database<br/>CDB + PDBs")]
        stby[("Physical standby<br/>mounted, redo apply")]
    end

    sim["Load generator<br/>optional, for demos"]

    user --> chat
    user --> dash
    chat -->|question| agent
    agent -->|tool calls over HTTP| mcp
    llm <-->|prompts + answers| agent
    mcp -->|read-only queries + plans| core
    chat -->|proposed plans| cards
    cards -->|after Approve| core
    cards -->|audit row| sqlite
    dash --> sqlite
    poller -->|checks + diagnostics| core
    poller --> sqlite
    core -->|SQL| pri
    core -->|SYSDBA, SSH for restarts| stby
    pri ==>|redo, ASYNC| stby
    sim -->|INSERT + COMMIT| pri
```

| Component | Where | Role |
| --- | --- | --- |
| Chat UI | `chat.py`, mounted at `/chat` by `main.py` | Conversation, model picker, tool steps, action buttons |
| Approval cards | `chat_actions.py` | Renders proposed fixes, runs them only after **Approve**, writes the audit row. Alternatives share a group; approving one withdraws the rest. |
| Agent | `agent_app/agent.py` | PydanticAI agent with the DBA instructions. Runs inside the web app process; its only toolset is the MCP server, reached over HTTP |
| MCP server | `mcp_server/server.py`, its own process on :9000 | 16 tools: 13 read-only diagnostics (one reads the monitor's incidents, one partition retention), `list_remediation_actions`, and two `propose_*` tools that return plans and execute nothing |
| Shared Oracle layer | `oracle_core/`, a library loaded by both processes | All SQL: `queries.py`, `queries_advanced.py` (ASH, Data Guard), `partitions.py` (monthly partition retention), `remediation.py` (allowlisted actions), `db.py` (python-oracledb thin connections). Reaches the standby as SYSDBA, and over SSH + `docker exec` for instance restarts. |
| Monitor | `monitor/` | Poller, 6 checks, approved diagnostics, LLM recommendation, SQLite lifecycle (ACTIVE / RESOLVED), dashboard |
| Header alert | `public/custom.js`, `public/custom.css` | Colours the Monitor link: green, amber, or solid red pulsing for an unacknowledged critical |
| Load generator | `scripts/ingest_simulator.py` | Optional demo write load, in its own schema |
| Tracing | `opentelemetry-instrument` + `.env.otel` | Agent, MCP server and monitor send OTLP traces to a collector such as Jaeger |
| Lab setup | `docker-stuff/00`–`03` | Optional scripts that build a primary and standby in Docker for testing |

## Asking a question, then fixing the problem

```mermaid
sequenceDiagram
    autonumber
    actor U as DBA
    participant C as Chat UI
    participant A as Agent
    participant L as LLM
    participant M as MCP server
    participant O as oracle_core
    participant DB as Oracle primary / standby
    participant S as monitor.db

    U->>C: Is my standby healthy?
    C->>A: question + conversation history
    A->>L: instructions, question, tool list
    L-->>A: call get_dataguard_status
    A->>M: tools/call
    M->>O: get_dataguard_status()
    O->>DB: role, lag, MRP0, gaps on both sides
    DB-->>O: rows
    O-->>M: report
    M-->>A: tool result
    A->>L: tool result
    L-->>A: answer
    A-->>C: streamed answer + tool steps
    U->>C: Can you fix it?
    A->>M: propose_remediation
    Note over M,O: planning only, nothing runs
    M-->>A: plan with statements, target, impact
    C-->>U: approval card
    U->>C: Approve
    C->>O: execute_remediation with the approved statements
    Note over O: re-plans and refuses if the statements changed
    O->>DB: run the allowlisted statements
    O-->>C: before, after, succeeded
    C->>S: audit row
    C-->>U: result
```

The model never writes SQL that runs. It picks an action from an allowlist
and supplies parameters; `oracle_core/remediation.py` builds the statements
from reviewed templates and validates every parameter. Execution happens in
the chat process after a human clicks Approve, and is never reachable through
MCP.

## The monitor cycle

```mermaid
sequenceDiagram
    participant P as Poller, every 15 s
    participant O as oracle_core
    participant DB as Primary database
    participant L as LLM
    participant S as monitor.db
    participant V as Dashboard + chat header

    loop every poll
        P->>O: run the 6 checks
        O->>DB: tablespace vs MAXSIZE, user-to-user blocking, long queries, alert log, waits, write rates
        DB-->>O: rows
    O-->>P: detected issues
        alt new, reopened or severity changed
            P->>O: approved diagnostic queries
            P->>L: measured facts + evidence
            L-->>P: root cause + recommended action
        end
        P->>S: record ACTIVE incidents, resolve the ones that cleared
    end
    V->>S: GET /monitor/api/issues every 15 s
    Note over V: red link for an unacknowledged critical
```

## Allowlisted changes

| Action | Target | Reversible | Runs through |
| --- | --- | --- | --- |
| `restart_redo_apply` | Standby | Yes | SYSDBA SQL |
| `restart_standby_instance` | Standby | No | SQL*Plus on the standby host over SSH, since thin mode cannot shut down or start an instance (the current implementation runs it inside a Docker container) |
| `enable_tablespace_autoextend` | Primary PDB tablespace | Yes | DDL in the PDB |
| `add_tablespace_datafile` | Primary PDB tablespace | No | DDL in the PDB; the standby creates the file itself (`standby_file_management=AUTO`) |
| `drop_old_partitions` | Primary table range-partitioned by month | No | `DROP PARTITION ... UPDATE INDEXES` in the PDB, keeping at least the current and previous month; permanently deletes the older months |

Every approval and rejection is recorded in `remediation_audit` in
`monitor.db`.

# Demo and test scenarios

Every situation the agent was built and tested against can be recreated in
a new environment with one program, `scripts/scenarios.py`. It needs only
the `.env` files: no Docker, SSH or SQL*Plus. Object locations such as
datafile directories come from the target database's own dictionary.

**Lab or test databases only.** The scenarios degrade the database on
purpose: they fill tablespaces, hold locks, stop redo apply and flood it
with commits. The program refuses to run unless `.env.loadgen` contains
`LAB_SCENARIOS_ENABLED=true`.

## 1. One-time setup

1. Deploy the agent as described in [DEPLOYMENT.md](DEPLOYMENT.md), and
   check it answers questions.
2. Create `.env.loadgen` from the template and fill it in:

   | Setting | Value |
   | --- | --- |
   | `LOADGEN_ORACLE_USER` | Optional. Schema the scenarios create and write to (default `LOADGEN`); never falls back to the agent's account |
   | `LOADGEN_ORACLE_PASSWORD` | Its password; `prepare` creates the user with it |
   | `LAB_SCENARIOS_ENABLED` | `true`, for this lab or test database only |
   | `SCENARIO_ADMIN_USER` / `SCENARIO_ADMIN_PASSWORD` | A DBA login for setup work: creating the user and tablespaces, `ALTER SYSTEM` log switches. Leave empty to use `ORACLE_USER` from `.env.mcp`, which works only if that account is a DBA (the least-privilege agent account from DEPLOYMENT.md is not) |

   The Data Guard scenarios also use `ORACLE_STANDBY_*` from `.env.mcp`.

3. Define a short command, from the repository root:

   ```bash
   # bash
   scen() { uv run --env-file .env.mcp --env-file .env.agent --env-file .env.loadgen \
                python scripts/scenarios.py "$@"; }
   ```

   ```powershell
   # PowerShell
   function scen { uv run --env-file .env.mcp --env-file .env.agent --env-file .env.loadgen python scripts/scenarios.py @args }
   ```

   `.env.agent` is included so the load scenarios read your monitor
   thresholds.

4. Create the schema and base tables:

   ```bash
   scen prepare     # user, grants, lock table, ingest table
   scen status      # what exists; standby apply state
   scen list        # all scenarios
   ```

Every command takes `--pdb <name>`; the default is
`ORACLE_DEFAULT_PDB_NAME`.

**Before each scenario:** start the MCP server and web app, open the chat
and the monitor dashboard, and wait one poll interval after the web app
starts. Start a **new chat** for each scenario.

## 2. The scenarios

Monitor incidents appear within one poll interval
(`MONITOR_POLL_INTERVAL_SECONDS`) and resolve on the first poll after the
condition clears.

### Load and sessions

| Scenario | Command | Monitor raises | Ask the agent | Expected answer or card |
| --- | --- | --- | --- | --- |
| Write warning | `scen write-load --level warning` | `WRITE_WORKLOAD` **WARNING**. The rate is set halfway between your warn and crit commits/s thresholds | *"Is there any unusual write activity right now?"* | High commit rate from sessions with module `ingest_simulator` |
| Write flood | `scen write-load --level critical` | `WRITE_WORKLOAD` **CRITICAL** (8 uncapped workers); the Monitor link turns red | *"What is generating load right now?"* | The `ingest_simulator` sessions and the `INSERT INTO INGEST_SIM_ORDERS` statement |
| Bursts | `scen write-load --level burst --duration 600` | The incident opens, resolves and reopens with each burst | | Incident lifecycle on the dashboard |
| Blocking | `scen blocking --seconds 120` | `BLOCKING_SESSION` WARNING (CRITICAL past 300 s of waiting) | *"Is anything blocked right now?"* | Blocker and waiter sessions (both the load schema), waiting on `enq: TX - row lock contention` |
| Long-running | `scen long-query --seconds 150` | `LONG_RUNNING_QUERY` once the session passes `MONITOR_LONG_RUNNING_QUERY_SECONDS` | *"Are there any long-running sessions?"* | The busy session, its sql_id and elapsed time |

`write-load` stops by itself after `--duration` seconds (default 300);
`Ctrl+C` stops it earlier.

### Space

| Scenario | Command | Monitor raises | Ask the agent | Expected cards |
| --- | --- | --- | --- | --- |
| Tablespace full | `scen tablespace-full` | `TABLESPACE_USAGE` **CRITICAL** for `SCEN_FULL_TS` (a 32 MB fixed-size tablespace written until ORA-1653) and an alert-log error | *"SCEN_FULL_TS in <PDB> is full, how can we fix it?"* | **Option 1** enable autoextend (green), **Option 2** add a datafile (red). Approving one withdraws the other |
| Partition pressure | `scen partition-pressure` | `TABLESPACE_USAGE` **CRITICAL** for `PART_DEMO_TS`: a monthly-partitioned table with twelve months of history, filled to 97% | *"PART_DEMO_TS in <PDB> is almost full, what can we do?"* | **Option 3** too: drop old partitions of `SALES_HISTORY`, keeping the current and previous month; marked as permanent data deletion. Approving it takes the tablespace from about 97% to roughly 25-40% (the current month's partition, which is kept, holds the rows the fill wrote) |

Running `partition-pressure` again only refills the current month; it does
not add more history. Details: [PARTITION_PRESSURE_DEMO.md](PARTITION_PRESSURE_DEMO.md).

### Data Guard (needs a standby)

| Scenario | Command | Ask the agent | Expected |
| --- | --- | --- | --- |
| Redo apply stopped | `scen dg-stop-apply` (cancels MRP0, then 5 log switches on the primary) | *"Is my Data Guard standby healthy?"*, then *"Can you fix it?"* | "No": MRP0 missing, received ahead of applied, apply lag growing, transport healthy. Then the green `restart_redo_apply` card; after approval MRP0 is back and the gap closes |
| Restart without the chat | `scen dg-start-apply` | | Redo apply running again |

`scen status` shows MRP0 and received / applied sequences at any time.

### History (needs the Diagnostics Pack licence)

| Scenario | Command | Ask the agent | Expected |
| --- | --- | --- | --- |
| Past activity | `scen history-burst --minutes 10`, then wait a few minutes | *"What was the database waiting on in the last 15 minutes?"*, then *"Which SQL was responsible?"* | The agent uses ASH history (`get_ash_activity`), not cumulative counters: commit waits such as `log file sync`, then the `INSERT INTO INGEST_SIM_ORDERS` sql_id |

## 3. Reset

```bash
scen cleanup-all --yes              # drops every scenario table and tablespace
scen cleanup-all --yes --drop-user  # also drops the load schema
```

Then `scen prepare` to start again. Nothing else in the database is
touched.

## 4. Notes

- **Windows:** everything runs in the foreground, so it behaves the same
  as on Linux. The underlying `ingest_simulator.py` background mode is for
  Linux and macOS.
- **ASM or OMF:** `tablespace-full` and `partition-pressure` create their
  tablespace next to the PDB's SYSTEM datafile. On ASM they stop with a
  message; create `SCEN_FULL_TS` (32 MB, `AUTOEXTEND OFF`) or
  `PART_DEMO_TS` (128 MB, `AUTOEXTEND OFF`, `UNIFORM SIZE 1M`) by hand and
  rerun.
- **Archive logs:** load and Data Guard scenarios generate redo on the
  primary and the standby. Make sure the archive destinations have room,
  or that an RMAN deletion policy is in place.
- **Tuning:** the monitor thresholds in `.env.agent` decide which level
  each load reaches. `write-load --level warning` adapts to them;
  `critical` assumes the database can do at least the critical commit rate
  with 8 sessions.

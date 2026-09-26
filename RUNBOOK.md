# Oracle DBA Agent — Operations Runbook

Everything needed to stop, start, and verify the stack.

Project root: `/data/alpoor/ORIG/oracle-dba-pydanticai-simple`

---

## Critical facts

Three things are **not** stored in any config file. Losing them costs an
evening.

1. The standby container must be created with `--init`. Without it, PID 1
   is `tail -f /dev/null`, which never reaps exited `MRP0` processes. They
   accumulate as zombies, Oracle keeps seeing recovery sessions that are
   already dead, and every attempt to restart redo apply fails with
   `ORA-16448`.
2. The standby container must be created with `--ip 172.23.0.3`, because
   that address is hardcoded in `ORACLE_STANDBY_DSN` in `.env.mcp`.
3. `$ORACLE_HOME` on the standby lives in the image, **not** the volume.
   Recreating the container destroys the listener config, tnsnames,
   spfile, and password file. The datafiles survive; the configuration
   does not.

`docker start` is safe. `docker run` is a rebuild.

---

## Components

| Component | What it is | Restarts itself? |
|---|---|---|
| `oracle19c` | Primary CDB, port 1521 | Yes |
| `oracle19c-stby` | Physical standby, port 1522 | **No** |
| MCP server | Oracle tools, port 9000 | No |
| Web app | Chat + monitor, port 8000 | No |

---

## Shutdown

Clean order: apps first, then the standby, then the primary.

```bash
# 1. Stop the app processes (Ctrl-C in each terminal), or:
lsof -ti:8000 -ti:9000 | xargs -r kill

# 2. Stop redo apply and shut the standby down cleanly
OH=/opt/oracle/product/19c/dbhome_1
docker exec -i oracle19c-stby bash -lc \
  "export ORACLE_HOME=$OH ORACLE_SID=ORCLCDB PATH=$OH/bin:\$PATH; sqlplus -s / as sysdba" <<'SQL'
ALTER DATABASE RECOVER MANAGED STANDBY DATABASE CANCEL;
SHUTDOWN IMMEDIATE;
EXIT;
SQL

# 3. Stop the containers
docker stop oracle19c-stby oracle19c
```

If step 2 hangs for more than two minutes, `SHUTDOWN ABORT` instead.
Nothing is lost: the primary retains all redo.

---

## Startup

### Step 1 — Containers

```bash
docker start oracle19c oracle19c-stby
docker ps
```

Confirm both are up and the standby still shows `0.0.0.0:1522->1521/tcp`.

### Step 2 — Wait for the primary

Its entrypoint opens the database automatically. Give it 45 seconds.

```bash
sleep 45

OH=/opt/oracle/product/19c/dbhome_1
docker exec -i oracle19c bash -lc \
  "export ORACLE_HOME=$OH ORACLE_SID=ORCLCDB PATH=$OH/bin:\$PATH; sqlplus -s / as sysdba" <<'SQL'
SELECT name, open_mode, database_role, log_mode FROM v$database;
EXIT;
SQL
```

Expect `READ WRITE`, `PRIMARY`, `ARCHIVELOG`.

### Step 3 — Bring up the standby

Nothing here is automatic.

```bash
OH=/opt/oracle/product/19c/dbhome_1
docker exec -i oracle19c-stby bash -lc \
  "export ORACLE_HOME=$OH ORACLE_SID=ORCLCDB PATH=$OH/bin:\$PATH; \
   lsnrctl start; sqlplus -s / as sysdba" <<'SQL'
STARTUP MOUNT;
ALTER DATABASE RECOVER MANAGED STANDBY DATABASE DISCONNECT FROM SESSION;
ALTER SYSTEM REGISTER;
SELECT database_role, open_mode FROM v$database;
SELECT process, status, sequence# FROM v$managed_standby WHERE process = 'MRP0';
EXIT;
SQL
```

Expect `PHYSICAL STANDBY` / `MOUNTED`, and `MRP0` with status
`APPLYING_LOG` or `WAIT_FOR_LOG`.

`ALTER SYSTEM REGISTER` matters — without it the listener has no dynamic
registration and the agent's standby connection fails.

### Step 4 — Clear the primary's destination

`log_archive_dest_2` goes to `ERROR` whenever the standby is unreachable
and does not retry on its own.

```bash
docker exec -i oracle19c bash -lc \
  "export ORACLE_HOME=$OH ORACLE_SID=ORCLCDB PATH=$OH/bin:\$PATH; sqlplus -s / as sysdba" <<'SQL'
ALTER SYSTEM SET log_archive_dest_state_2='DEFER' SCOPE=BOTH;
ALTER SYSTEM SET log_archive_dest_state_2='ENABLE' SCOPE=BOTH;
ALTER SYSTEM SWITCH LOGFILE;
SELECT dest_id, status, gap_status, error FROM v$archive_dest_status WHERE dest_id = 2;
EXIT;
SQL
```

Expect `VALID`, `NO GAP`, no error.

### Step 5 — Verify Data Guard end to end

```bash
cd /data/alpoor/ORIG/oracle-dba-pydanticai-simple

uv run --env-file .env.mcp python -c "
from oracle_core.queries_advanced import get_dataguard_status
d = get_dataguard_status()['standby']
print('mrp0:', [p for p in d['apply_processes'] if p['process']=='MRP0'] or 'ABSENT')
print('lag:', [(l['name'], l['value']) for l in d['lag']])
print('srl:', len(d['standby_redo_logs']))
"
```

Expect `MRP0 APPLYING_LOG`, both lags `+00 00:00:00`, and 4 standby redo
logs. This also proves the agent can reach the standby over
`ORACLE_STANDBY_DSN`.

### Step 6 — MCP server (terminal 1)

Must start before the web app, which connects to it at startup.

```bash
cd /data/alpoor/ORIG/oracle-dba-pydanticai-simple

uv run \
  --env-file .env.otel --env-file .env.mcp \
  python -m mcp_server.server
```

Leave running. Oracle errors from tool calls appear here.

### Step 7 — Web app (terminal 2)

```bash
cd /data/alpoor/ORIG/oracle-dba-pydanticai-simple

uv run \
  --env-file .env.otel --env-file .env.mcp --env-file .env.agent \
  opentelemetry-instrument \
  uvicorn main:app --host 127.0.0.1 --port 8000
```

All three env files are required: `.env.agent` for the model, `.env.mcp`
for the standby credentials used by remediation, `.env.otel` for tracing.

### Step 8 — Open it

```
http://127.0.0.1:8000/chat      chat
http://127.0.0.1:8000/monitor/  dashboard
```

From a laptop, tunnel first — there is no authentication on this app:

```bash
ssh -L 8000:localhost:8000 apreddy@z11devrm473
```

### Step 9 — Smoke test

Ask: **"Is my Data Guard standby healthy?"**

A good answer names MRP0 as running, quotes zero lag, and stops there
without inventing a recommendation.

---

## Troubleshooting

**Agent says the standby cannot be read**
Listener registration was lost. Run `ALTER SYSTEM REGISTER;` on the
standby, then retry step 5.

**`ORA-16448` when restarting redo apply**
Check for zombies: `docker exec oracle19c-stby ps -ef | grep defunct`.
Anything there means the container is missing `--init` and needs
recreating (see below). If there are none, use the
`restart_standby_instance` remediation.

**Model picker missing from the chat**
OpenWebUI was unreachable at page load. Check the uvicorn console for
`Warning: could not load OpenWebUI model list`, then reload the page.

**Monitor link never lights up**
Open the browser console and check `/monitor/api/issues` returns data.
The poller runs every 30 seconds.

**Standby connection hangs instead of erroring**
The listener is not on `0.0.0.0`, or `DEDICATED_THROUGH_BROKER_LISTENER`
is missing from `listener.ora`. Both are required for host-side access
through the mapped port.

---

## If the standby container must be recreated

Back up the configuration **first** — it is not on the volume:

```bash
mkdir -p stby-backup
OH=/opt/oracle/product/19c/dbhome_1
for f in network/admin/listener.ora network/admin/tnsnames.ora \
         dbs/spfileORCLCDB.ora dbs/orapwORCLCDB dbs/initORCLCDB.ora; do
  docker cp "oracle19c-stby:$OH/$f" "stby-backup/$(basename $f)"
done
```

Recreate with both required flags:

```bash
docker run -d \
  --name oracle19c-stby \
  --hostname oracle19c-stby \
  --network oracle-demo \
  --init \
  --ip 172.23.0.3 \
  -p 1522:1521 \
  -e ORACLE_SID=ORCLCDB \
  -e ORACLE_BASE=/opt/oracle \
  -e ORACLE_HOME=/opt/oracle/product/19c/dbhome_1 \
  -v oracle19c-stby-data:/opt/oracle/oradata \
  --entrypoint /bin/bash \
  oracle/database:19.3.0-ee-ext -c 'tail -f /dev/null'
```

Then restore the files from `stby-backup/`, or re-run
`rebuild-standby-home.sh` if the backup is missing.

---

## Testing the agent

`dg-chaos.sh` injects reversible faults.

```bash
./dg-chaos.sh status           # health, both sides
./dg-chaos.sh stop-apply       # MRP0 gone, transport still healthy
./dg-chaos.sh redo             # generate redo to build a gap
./dg-chaos.sh restore-all      # put it all back
```

`stop-apply` is the most valuable test: transport lag stays at zero and
every destination stays `VALID`, so only the missing `MRP0` and the
growing sequence gap reveal the problem. An agent that pattern-matches on
lag will get it wrong.

Clean up afterwards — `redo` leaves a few hundred MB behind:

```bash
docker exec -i oracle19c bash -lc \
  "export ORACLE_HOME=$OH ORACLE_SID=ORCLCDB PATH=$OH/bin:\$PATH; sqlplus -s / as sysdba" <<'SQL'
DROP TABLE dg_chaos_test PURGE;
EXIT;
SQL
```

## Audit trail

Every approved or rejected remediation is recorded in `monitor.db`.

The `sqlite3` CLI is not installed on this host and does not need to be —
Python ships with SQLite:

```bash
cd /data/alpoor/ORIG/oracle-dba-pydanticai-simple

uv run python -c "
import sqlite3
conn = sqlite3.connect('monitor.db')
for row in conn.execute(
    'SELECT id, action, outcome, approved_by, requested_at '
    'FROM remediation_audit ORDER BY id'
):
    print(row)
"
```

With detail, including why any failures failed:

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('monitor.db')
conn.row_factory = sqlite3.Row
for r in conn.execute('SELECT * FROM remediation_audit ORDER BY id'):
    print(f\"#{r['id']} {r['action']} -> {r['outcome']} \"
          f\"by {r['approved_by']} at {r['requested_at']}\")
    if r['error']:
        print('   error:', r['error'][:140])
"
```

Outcomes are `APPROVED` (ran), `REJECTED` (declined, nothing ran), or
`FAILED` (approved but errored). Rejections are recorded deliberately — a
refusal is as much a decision as an approval.

The same approach works for the issues table:

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('monitor.db')
conn.row_factory = sqlite3.Row
for r in conn.execute(
    'SELECT id, issue_type, severity, status, acknowledged, summary '
    'FROM issues ORDER BY id DESC LIMIT 20'
):
    print(dict(r))
"
```

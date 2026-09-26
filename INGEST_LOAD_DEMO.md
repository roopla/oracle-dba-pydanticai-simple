# Oracle High-Insert Load Demo

This demo simulates the Oracle-side workload produced by a fast upstream ingestion system. RabbitMQ is not required.

## 1. Create the table and run a safe first test

```bash
uv run \
  --env-file .env.mcp \
  python scripts/oracle_insert_load.py \
    --setup-table \
    --workers 2 \
    --batch-size 50 \
    --duration 30
```

## 2. Increase the load

```bash
uv run \
  --env-file .env.mcp \
  python scripts/oracle_insert_load.py \
    --workers 10 \
    --batch-size 250 \
    --payload-bytes 1000 \
    --duration 120
```

Each worker has its own Oracle session. Each batch is inserted using `executemany()` and committed once.

## 3. Confirm the generated rows

```sql
SELECT COUNT(*) AS row_count,
       MIN(created_at) AS first_insert,
       MAX(created_at) AS last_insert
FROM app_ingest_demo;
```

Inspect the workload sessions while the script is running:

```sql
SELECT sid,
       serial#,
       username,
       module,
       action,
       status,
       event,
       wait_class,
       sql_id
FROM v$session
WHERE module = 'oracle_insert_load';
```

## 4. Clean up after the demo

```sql
TRUNCATE TABLE app_ingest_demo;
```

To remove it completely:

```sql
DROP TABLE app_ingest_demo PURGE;
```

## Important monitoring note

The current dashboard checks tablespace usage, blockers, long-running sessions, alert-log errors, and cumulative wait events. A fast insert workload may not reliably create an incident by itself. The next enhancement should add a throughput check based on `V$SYSSTAT` deltas, such as redo bytes/sec, executions/sec, user commits/sec, and user calls/sec.

## Write-workload incident

The monitor now calculates interval rates from `V$SYSSTAT` and creates one
`WRITE_WORKLOAD` incident when any configured warning or critical threshold is
crossed. The first successful poll only establishes a baseline, so start the
web application first and allow one poll cycle before launching the load.

Default thresholds (override in `.env.agent`):

```bash
MONITOR_WRITE_WARN_COMMITS_PER_SEC=40
MONITOR_WRITE_CRIT_COMMITS_PER_SEC=150
MONITOR_WRITE_WARN_REDO_MB_PER_SEC=5
MONITOR_WRITE_CRIT_REDO_MB_PER_SEC=25
MONITOR_WRITE_WARN_EXECUTES_PER_SEC=2000
MONITOR_WRITE_CRIT_EXECUTES_PER_SEC=10000
```

Recommended demo sequence:

```bash
# Terminal 1: start the application and monitor
uv run \
  --env-file .env.otel \
  --env-file .env.mcp \
  --env-file .env.agent \
  opentelemetry-instrument \
  uvicorn agent_app.web_combined:app --host 127.0.0.1 --port 8000

# Wait for at least one monitor polling cycle, then Terminal 2:
PYTHONPATH=. uv run \
  --env-file .env.mcp \
  python scripts/oracle_insert_load.py \
    --workers 5 \
    --batch-size 100 \
    --payload-bytes 1000 \
    --duration 180
```

Open `http://127.0.0.1:8000/monitor/`. With the default 60-second poll interval,
keep the workload running for at least two minutes so the monitor has a baseline
sample and a second sample from which to calculate rates.

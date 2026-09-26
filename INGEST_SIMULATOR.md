# Ingest Simulator: fast-ingest load on Oracle

`scripts/ingest_simulator.py` reproduces what happens when an upstream feed, such as our RabbitMQ consumers, pushes rows into Oracle faster than the database can handle. It runs in the background, and you control it with `start`, `status` and `stop` while you watch the monitor dashboard react.

It writes directly to Oracle; RabbitMQ is not involved. Each worker thread holds its own Oracle session and behaves like one consumer instance. By default, each worker inserts one message and commits it, exactly like `rabbitmq-oracle-demo/consumer.py`.

## One-time setup

**1. Create a dedicated schema.** A DBA runs this once in the PDB. The simulator refuses to run as SYS or SYSTEM, because the demo table would otherwise land in the SYSTEM tablespace.

```bash
sqlplus system@//localhost:1521/ORCLPDB1.localdomain @scripts/ingest_simulator_user.sql
```

**2. Point the simulator at that schema.**

```bash
cp .env.loadgen.example .env.loadgen      # edit the password if you changed it
```

**3. Create the table.**

```bash
uv run --env-file .env.mcp --env-file .env.loadgen \
  python scripts/ingest_simulator.py setup
```

This creates `LOADGEN.INGEST_SIM_ORDERS` with a primary key, a unique `MESSAGE_ID` constraint and an index on `CREATED_AT`. The `CREATED_AT` index is deliberate: an ever-increasing index key under concurrent inserts is a common real-world cause of ingest slowdowns.

## Everyday commands

To keep commands short, define an alias in your shell:

```bash
alias sim='uv run --env-file .env.mcp --env-file .env.loadgen python scripts/ingest_simulator.py'
```

| Command | What it does |
|---|---|
| `sim start --mode steady` | Starts in the background and returns immediately |
| `sim status` | Shows phase, active workers, rows/s, commits/s, average commit latency and errors |
| `sim status --json` | The same information as JSON, for scripts |
| `sim stop` | Graceful stop. Workers finish or roll back their current transaction, then close their sessions |
| `sim cleanup` | Truncates the table (`--drop` drops it instead) |

`status` and `stop` do not need any environment files.

Useful details:
- **Automatic stop.** Every run stops after `--duration` seconds (default 600), so a forgotten test cannot run all night. Use `--duration 0` to run until you stop it yourself.
- **Runtime files.** The log, PID file and state file live in `.ingest_sim/`. Follow the log live with `tail -f .ingest_sim/ingest_sim.log`.
- **Terminal independence.** The process keeps running if you close your terminal.
- **One run at a time.** A second `start` is refused while a run is active.
- **Session tagging.** Sessions show up in `V$SESSION` as `MODULE='ingest_simulator'`, so the chat agent and DBAs can see exactly where the load comes from.

## The three load modes

| Mode | Behaviour | Key options |
|---|---|---|
| `steady` | Constant load from all workers | `--workers`, `--rate` (total rows/s, 0 = no cap) |
| `burst` | Full load, then idle, repeating | `--burst-seconds` (45), `--pause-seconds` (60) |
| `ramp` | Adds workers step by step to find the breaking point | `--start-workers`, `--step-workers`, `--step-seconds` (30), `--stop-latency-ms` |

There are also two commit modes:
- **`--commit-mode row`** (default) commits after every message. This gives a very high commits/s rate and `log file sync` waits.
- **`--commit-mode batch`** inserts `--batch-size` rows per commit. This gives fewer commits but much more redo per second.

## Demo recipes

These recipes use the monitor thresholds currently set in `.env.agent`:

| Metric | Warning | Critical |
|---|---|---|
| Commits/s | 20 | 100 |
| Redo MB/s | 2 | 15 |
| Executes/s | 1000 | 7000 |

The poll interval is 15 seconds.

**Always start the web app first and wait about 30 seconds before starting load.** The write-workload check needs one poll as a baseline before it can calculate rates.

### 1. Warning incident: moderate, controlled load

```bash
sim start --mode steady --workers 4 --rate 60
```

This produces about 60 commits/s, which is between the warning and critical thresholds. Expect a **WARNING** `WRITE_WORKLOAD` incident within about two polls.

### 2. Critical incident: the feed floods the database

```bash
sim start --mode steady --workers 8
```

With no rate cap and a commit per message, commits/s will far exceed 100, so expect a **CRITICAL** incident. Then ask the chat agent: *"What is generating load right now?"* It should find the `ingest_simulator` sessions and the top INSERT statement.

### 3. Burst: incident opens, resolves and reopens

```bash
sim start --mode burst --workers 8 --burst-seconds 45 --pause-seconds 60
```

This shows the full incident lifecycle on the dashboard. Keep `--pause-seconds` at least twice the poll interval; otherwise the monitor never samples a quiet period and the incident never resolves.

### 4. Ramp: where does this database start to struggle?

```bash
sim start --mode ramp --workers 16 --step-seconds 30 --stop-latency-ms 10
```

The simulator adds one worker every 30 seconds. At the end of each step it logs the throughput and the average commit latency. Saturation looks like this: rows/s stops increasing while commit latency keeps climbing. `--stop-latency-ms` stops the run automatically at that point. Read the results with `tail .ingest_sim/ingest_sim.log`.

### 5. Redo-heavy batch ingest

```bash
sim start --mode steady --commit-mode batch --batch-size 500 --payload-bytes 3000 --workers 6
```

This run has few commits but a lot of redo, so it should trip the **redo MB/s** threshold rather than commits/s. It is useful for showing that the monitor distinguishes between the two kinds of load.

## Safety notes

- **Use non-production databases only.** Every mode except a rate-capped steady run is designed to degrade the database on purpose.
- **Data Guard impact.** Redo-heavy runs also load the standby through redo transport and apply. That is realistic, but warn whoever owns the standby.
- **Space cap.** The `QUOTA 2G ON USERS` in the setup script limits how much space the demo can use. If the quota is reached, workers log `ORA-01536`, retry with backoff, and each worker gives up after 25 consecutive errors. Raise the quota if you also want to demo a tablespace-full incident.
- **Full cleanup.** After the demo, run `sim cleanup --drop --yes`, or have a DBA run `DROP USER LOADGEN CASCADE;`.
- **Test without Oracle.** Add `--dry-run` to any command to exercise the start/stop/status mechanics without touching Oracle.

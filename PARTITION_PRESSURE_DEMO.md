# Partition retention under space pressure

Tables range-partitioned by month grow until their tablespace runs short.
The usual fix is to drop the oldest months while keeping the recent ones.
The agent can offer that as an approved change, `drop_old_partitions`,
next to the other tablespace fixes (autoextend, add a datafile).

`scripts/partition_pressure_simulator.py` builds the situation in a lab
database so you can watch the monitor raise it and the agent resolve it.
The simplest way to run it is `scripts/scenarios.py partition-pressure`
(see [SCENARIOS.md](SCENARIOS.md)), which runs the steps below for you.

## What the action does

- **Eligible tables:** range-partitioned on one `DATE` or `TIMESTAMP`
  column with monthly boundaries (plain range or monthly `INTERVAL`), owned
  by an application schema. Composite-partitioned tables and tables owned
  by Oracle-maintained schemas are left alone.
- **Retention:** keeps the newest `keep_months` months (default 2, minimum
  2: the current and the previous month) and, as a second guard, the two
  newest partitions whatever their dates. `MAXVALUE` partitions are never
  dropped. "Current month" is the database's month, not the client's.
- **Statements:** one `ALTER TABLE ... DROP PARTITION ... UPDATE INDEXES`
  per partition, so global indexes stay usable. For interval-partitioned
  tables it first runs `ALTER TABLE ... SET INTERVAL (<the table's own
  interval>)`; without it Oracle refuses to drop the last partition of the
  range section (ORA-14758).
- **Safety:** the approval card lists the months dropped and kept and the
  space freed, and is marked **NOT reversible**: the rows can only be
  recovered from a backup. If the partition list changes between approval
  and execution, execution refuses and asks for a fresh proposal.

## Running the demo

Needs the `LOADGEN` schema from `scripts/ingest_simulator_user.sql` and
`.env.loadgen`. Tablespace DDL uses the DBA account in `.env.mcp`.

```bash
sim_part() { uv run --env-file .env.mcp --env-file .env.loadgen \
                 python scripts/partition_pressure_simulator.py "$@"; }
```

PowerShell:

```powershell
function sim_part { uv run --env-file .env.mcp --env-file .env.loadgen python scripts/partition_pressure_simulator.py @args }
```

| Step | Command | Result |
| --- | --- | --- |
| 1 | `sim_part setup` | Fixed-size 128 MB tablespace `PART_DEMO_TS` (uniform 1 MB extents) and `LOADGEN.SALES_HISTORY`, interval-partitioned by month, with a global primary key and a local index |
| 2 | `sim_part backfill --months 12` | Twelve past months, about 6 MB each; the tablespace ends near 70% |
| 3 | `sim_part fill --target-pct 97` | Current-month rows until 97% used. Add `--until-error` to keep going until Oracle refuses with ORA-1653 / ORA-1688, as production would. |
| 4 | `sim_part status` | Tablespace usage and each partition's month and size |

Then, with the web app running:

1. The monitor raises a CRITICAL `TABLESPACE_USAGE` incident for
   `PART_DEMO_TS` (fixed size, so near 100% of its maximum), and the
   Monitor link turns red.
2. Ask in the chat: *"PART_DEMO_TS in ORCLPDB1 is almost full, what can we
   do?"* The agent offers three cards: **Option 1** enable autoextend,
   **Option 2** add a datafile, **Option 3** drop old partitions of
   `LOADGEN.SALES_HISTORY`, and says that option 3 deletes data.
3. Approve option 3. The result shows the partitions and tablespace usage
   before and after; with twelve months of history, eleven partitions go
   and the tablespace drops from about 97% to roughly 25-40% (the current month's partition, which is kept, holds the rows the fill wrote). The incident
   resolves on the next poll.

To look without proposing anything, ask *"Which partitioned tables in
ORCLPDB1 hold data older than two months?"*; the agent answers with
`get_partition_retention`.

`sim_part cleanup --yes` drops the table and the tablespace.

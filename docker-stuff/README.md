# Data Guard standby for the ORCLCDB Docker primary

Builds `ORCLCDB_STBY` in a second container, applying redo from your
existing `oracle19c` primary. Gives `get_dataguard_status` something real
to report.

## Layout

| | Primary | Standby |
|---|---|---|
| Container | `oracle19c` | `oracle19c-stby` |
| `db_name` | ORCLCDB | ORCLCDB |
| `db_unique_name` | ORCLCDB | ORCLCDB_STBY |
| Volume | `oracle19c-data` | `oracle19c-stby-data` |
| Host port | 1521 | 1522 |
| SGA | ~2.3G | 1G |

Both use identical file paths inside their own containers, so no
`db_file_name_convert` is needed and role transitions stay simple.

## Before you start

Free memory first — you have 4.4Gi available and are already 1.8Gi into
swap:

```bash
docker stop dba-lab-control-plane dba-lab-worker dba-lab-worker2
free -h
```

## Run

```bash
chmod +x 02-create-standby.sh 03-duplicate.sh
export SYS_PASSWORD='your_sys_password'

# 1. Prepare the primary
docker cp 01-primary-prep.sql oracle19c:/tmp/
docker exec -it oracle19c bash -lc \
  'sqlplus / as sysdba @/tmp/01-primary-prep.sql'

# 2. Build the standby container (fast)
./02-create-standby.sh

# 3. RMAN duplicate + start apply (slow, heavy I/O)
./03-duplicate.sh
```

If step 1 stops saying the database is in NOARCHIVELOG, run the four
statements it prints, then re-run it. Every script is safe to re-run.

## Wiring it into the agent

The standby is a **different host:port** from the primary, so the
`ORACLE_STANDBY_NAME` env var alone is not enough — `oracle_core.db.build_dsn`
always uses `oracle_host` and `oracle_port`. Add a full DSN instead, in
`.env.mcp`:

```
ORACLE_STANDBY_DSN=<docker_host>:1522/ORCLCDB_STBY
```

`queries_advanced.py` needs a small patch to use it — ask and I'll send
the diff. Until then `get_dataguard_status` reports primary-side data
only, and says so via `standby_note`.

## Testing the tool

The point of having a standby is being able to break it on purpose:

```sql
-- On the standby: stop apply
ALTER DATABASE RECOVER MANAGED STANDBY DATABASE CANCEL;
```

Generate some redo on the primary, then ask the agent about Data Guard
status. It should report a growing `sequence_gap` and a missing `MRP0`.
Restart apply with:

```sql
ALTER DATABASE RECOVER MANAGED STANDBY DATABASE
  USING CURRENT LOGFILE DISCONNECT FROM SESSION;
```

## Notes and gotchas

- **Redo apply only.** Opening the standby read-only while applying is
  Active Data Guard, a separately licensed option. Mounted apply needs no
  extra licence and works identically for the tool.
- **Password file must match.** Step 2 copies it. If you change the SYS
  password later, copy it again or transport stops with ORA-16191.
- **Restart order.** After a host reboot the standby container comes up
  with a plain bash entrypoint, so nothing starts automatically. Start
  the listener, `STARTUP MOUNT`, then restart redo apply.
- **Not a performance test rig.** The 1G SGA means the standby is sized
  to prove the configuration works, not to stand in for the primary.

#!/usr/bin/env bash
# ---------------------------------------------------------------------
# dg-chaos.sh - break Data Guard on purpose, then put it back.
#
# Every scenario is reversible and none of them lose data: the primary
# retains all redo, and the standby catches up once restored.
#
# Usage:
#     ./dg-chaos.sh status          # current health, both sides
#     ./dg-chaos.sh redo            # generate redo + log switches
#
#     ./dg-chaos.sh stop-apply      # scenario 1: MRP0 gone
#     ./dg-chaos.sh start-apply
#
#     ./dg-chaos.sh stop-transport  # scenario 2: redo stops shipping
#     ./dg-chaos.sh start-transport
#
#     ./dg-chaos.sh stop-listener   # scenario 3: standby unreachable
#     ./dg-chaos.sh start-listener
#
#     ./dg-chaos.sh restore-all     # put everything back
# ---------------------------------------------------------------------

set -uo pipefail

PRIMARY="oracle19c"
STANDBY="oracle19c-stby"
OH="/opt/oracle/product/19c/dbhome_1"
ENVSET="export ORACLE_HOME=$OH ORACLE_SID=ORCLCDB PATH=$OH/bin:\$PATH"

sql_primary() {
    docker exec -i "$PRIMARY" bash -lc "$ENVSET; sqlplus -s / as sysdba"
}

sql_standby() {
    docker exec -i "$STANDBY" bash -lc "$ENVSET; sqlplus -s / as sysdba"
}

case "${1:-}" in

status)
    echo "===== PRIMARY ====="
    sql_primary <<'SQL'
SET LINESIZE 200 PAGESIZE 60
COL dest_name FORMAT A22
COL error FORMAT A34
SELECT dest_id, dest_name, status, gap_status, error
FROM v$archive_dest_status WHERE status <> 'INACTIVE';
SELECT thread#, MAX(sequence#) last_seq FROM v$archived_log GROUP BY thread#;
EXIT;
SQL
    echo
    echo "===== STANDBY ====="
    sql_standby <<'SQL'
SET LINESIZE 200 PAGESIZE 60
SELECT database_role, open_mode FROM v$database;
SELECT process, status, sequence# FROM v$managed_standby
WHERE process IN ('MRP0','RFS') ORDER BY process;
SELECT name, value FROM v$dataguard_stats
WHERE name IN ('transport lag','apply lag');
SELECT recovery_mode FROM v$archive_dest_status WHERE dest_id = 1;
EXIT;
SQL
    ;;

redo)
    # Enough redo to make a gap visible, plus forced log switches.
    echo "==> Generating redo on the primary"
    sql_primary <<'SQL'
SET SERVEROUTPUT ON
DECLARE
    v_count NUMBER;
BEGIN
    BEGIN
        EXECUTE IMMEDIATE 'DROP TABLE dg_chaos_test PURGE';
    EXCEPTION WHEN OTHERS THEN NULL;
    END;

    EXECUTE IMMEDIATE
        'CREATE TABLE dg_chaos_test AS SELECT * FROM all_objects';

    FOR i IN 1 .. 3 LOOP
        EXECUTE IMMEDIATE
            'INSERT INTO dg_chaos_test SELECT * FROM dg_chaos_test';
        COMMIT;
    END LOOP;

    -- Dynamic: the table does not exist when this block is compiled.
    EXECUTE IMMEDIATE 'SELECT COUNT(*) FROM dg_chaos_test' INTO v_count;
    DBMS_OUTPUT.PUT_LINE('rows: ' || v_count);
END;
/
ALTER SYSTEM SWITCH LOGFILE;
ALTER SYSTEM SWITCH LOGFILE;
ALTER SYSTEM SWITCH LOGFILE;
SELECT thread#, MAX(sequence#) last_seq FROM v$archived_log GROUP BY thread#;
EXIT;
SQL
    ;;

stop-apply)
    # Scenario 1: redo still arrives, nothing applies it.
    # The dangerous one - transport looks perfectly healthy.
    echo "==> Cancelling redo apply on the standby (takes ~60s)"
    sql_standby <<'SQL'
ALTER DATABASE RECOVER MANAGED STANDBY DATABASE CANCEL;
EXIT;
SQL
    sleep 5
    sql_standby <<'SQL'
SELECT process, status FROM v$managed_standby WHERE process LIKE 'MRP%';
SELECT recovery_mode FROM v$archive_dest_status WHERE dest_id = 1;
EXIT;
SQL
    echo "MRP0 should now be absent and recovery_mode IDLE."
    echo "Run './dg-chaos.sh redo' then ask the agent about standby health."
    ;;

start-apply)
    # A clean bounce is far more reliable than fighting ORA-16448.
    echo "==> Restarting the standby instance and redo apply"
    sql_standby <<'SQL'
SHUTDOWN ABORT;
STARTUP MOUNT;
ALTER DATABASE RECOVER MANAGED STANDBY DATABASE DISCONNECT FROM SESSION;
EXIT;
SQL
    sleep 20
    docker exec -i "$STANDBY" bash -lc "$ENVSET; sqlplus -s / as sysdba" <<'SQL'
ALTER SYSTEM REGISTER;
SELECT process, status, sequence# FROM v$managed_standby WHERE process = 'MRP0';
SELECT recovery_mode FROM v$archive_dest_status WHERE dest_id = 1;
EXIT;
SQL
    ;;

stop-transport)
    # Scenario 2: apply is fine, but no new redo arrives.
    echo "==> Deferring log_archive_dest_2 on the primary"
    sql_primary <<'SQL'
ALTER SYSTEM SET log_archive_dest_state_2='DEFER' SCOPE=BOTH;
SELECT dest_id, status FROM v$archive_dest_status WHERE dest_id = 2;
EXIT;
SQL
    echo "Run './dg-chaos.sh redo' to build a real gap, then ask the agent."
    ;;

start-transport)
    echo "==> Re-enabling log_archive_dest_2"
    sql_primary <<'SQL'
ALTER SYSTEM SET log_archive_dest_state_2='ENABLE' SCOPE=BOTH;
ALTER SYSTEM SWITCH LOGFILE;
EXIT;
SQL
    sleep 10
    sql_primary <<'SQL'
SELECT dest_id, status, gap_status, error FROM v$archive_dest_status
WHERE dest_id = 2;
EXIT;
SQL
    ;;

stop-listener)
    # Scenario 3: standby unreachable. Also breaks the agent's own
    # standby connection, so get_dataguard_status falls back to
    # primary-side data and should say so.
    echo "==> Stopping the standby listener"
    docker exec "$STANDBY" bash -lc "$ENVSET; lsnrctl stop"
    echo "Ask the agent about standby health - it should report the"
    echo "destination error AND admit it could not read the standby."
    ;;

start-listener)
    echo "==> Starting the standby listener"
    docker exec "$STANDBY" bash -lc "$ENVSET; lsnrctl start"
    sql_standby <<'SQL'
ALTER SYSTEM REGISTER;
EXIT;
SQL
    sleep 5
    sql_primary <<'SQL'
ALTER SYSTEM SET log_archive_dest_state_2='DEFER' SCOPE=BOTH;
ALTER SYSTEM SET log_archive_dest_state_2='ENABLE' SCOPE=BOTH;
ALTER SYSTEM SWITCH LOGFILE;
EXIT;
SQL
    ;;

restore-all)
    echo "==> Restoring everything"
    docker exec "$STANDBY" bash -lc "$ENVSET; lsnrctl start" || true
    "$0" start-apply
    "$0" start-transport
    sleep 10
    "$0" status
    ;;

*)
    sed -n '3,26p' "$0"
    exit 1
    ;;
esac

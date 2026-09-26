#!/usr/bin/env bash
set -euo pipefail

PRIMARY="oracle19c"
STANDBY="oracle19c-stby"
OH="/opt/oracle/product/19c/dbhome_1"
SID="ORCLCDB"
STBY_UNIQUE="ORCLCDB_STBY"
DOMAIN="localdomain"
ENVSET="export ORACLE_HOME=$OH ORACLE_SID=$SID PATH=$OH/bin:\$PATH"

: "${SYS_PASSWORD:?Set SYS_PASSWORD before running}"

echo "==> Directories"
docker exec "$STANDBY" bash -lc "
  mkdir -p /opt/oracle/admin/$SID/adump \
           /opt/oracle/fast_recovery_area/$SID \
           $OH/network/admin $OH/dbs/arch"

echo "==> Password file from primary"
docker exec "$PRIMARY" bash -lc "cat $OH/dbs/orapw$SID" > /tmp/orapw_copy
docker cp /tmp/orapw_copy "$STANDBY:$OH/dbs/orapw$SID"
rm -f /tmp/orapw_copy
docker exec -u 0 "$STANDBY" bash -lc \
  "chown oracle:oinstall $OH/dbs/orapw$SID && chmod 640 $OH/dbs/orapw$SID"

echo "==> listener.ora"
docker exec "$STANDBY" bash -lc "cat > $OH/network/admin/listener.ora <<'EOF'
LISTENER =
  (DESCRIPTION_LIST =
    (DESCRIPTION =
      (ADDRESS = (PROTOCOL = TCP)(HOST = 0.0.0.0)(PORT = 1521))
      (ADDRESS = (PROTOCOL = IPC)(KEY = EXTPROC1521))
    )
  )

SID_LIST_LISTENER =
  (SID_LIST =
    (SID_DESC =
      (GLOBAL_DBNAME = ${STBY_UNIQUE}.${DOMAIN})
      (ORACLE_HOME = ${OH})
      (SID_NAME = ${SID})
    )
    (SID_DESC =
      (GLOBAL_DBNAME = ${STBY_UNIQUE}_DGMGRL.${DOMAIN})
      (ORACLE_HOME = ${OH})
      (SID_NAME = ${SID})
    )
  )

DEDICATED_THROUGH_BROKER_LISTENER=ON
DIAG_ADR_ENABLED=off
EOF"

echo "==> tnsnames.ora on both"
for c in "$PRIMARY" "$STANDBY"; do
docker exec "$c" bash -lc "cat > $OH/network/admin/tnsnames.ora <<'EOF'
ORCLCDB =
  (DESCRIPTION =
    (ADDRESS = (PROTOCOL = TCP)(HOST = ${PRIMARY})(PORT = 1521))
    (CONNECT_DATA = (SERVER = DEDICATED)(SERVICE_NAME = ${SID}.${DOMAIN}))
  )

${STBY_UNIQUE} =
  (DESCRIPTION =
    (ADDRESS = (PROTOCOL = TCP)(HOST = ${STANDBY})(PORT = 1521))
    (CONNECT_DATA = (SERVER = DEDICATED)(SERVICE_NAME = ${STBY_UNIQUE}.${DOMAIN}))
  )
EOF"
done

echo "==> Listener"
docker exec "$STANDBY" bash -lc "$ENVSET; lsnrctl start" || true

echo "==> init pfile"
docker exec "$STANDBY" bash -lc "cat > $OH/dbs/init$SID.ora <<EOF
db_name='$SID'
db_unique_name='$STBY_UNIQUE'
control_files='/opt/oracle/oradata/$SID/control01.ctl'
sga_target=1536M
sga_max_size=1536M
pga_aggregate_target=256M
db_block_size=8192
audit_file_dest='/opt/oracle/admin/$SID/adump'
enable_pluggable_database=TRUE
log_archive_config='DG_CONFIG=(ORCLCDB,$STBY_UNIQUE)'
log_archive_dest_1='LOCATION=$OH/dbs/arch VALID_FOR=(ALL_LOGFILES,ALL_ROLES) DB_UNIQUE_NAME=$STBY_UNIQUE'
log_archive_dest_2='SERVICE=ORCLCDB ASYNC VALID_FOR=(ONLINE_LOGFILES,PRIMARY_ROLE) DB_UNIQUE_NAME=ORCLCDB'
log_archive_dest_state_2='ENABLE'
fal_server='ORCLCDB'
standby_file_management='AUTO'
remote_login_passwordfile='EXCLUSIVE'
EOF"

echo "==> spfile + mount"
docker exec -i "$STANDBY" bash -lc "$ENVSET; sqlplus -s / as sysdba" <<SQL
CREATE SPFILE='$OH/dbs/spfile$SID.ora' FROM PFILE='$OH/dbs/init$SID.ora';
STARTUP MOUNT;
ALTER SYSTEM REGISTER;
SELECT name, db_unique_name, database_role, open_mode FROM v\$database;
EXIT;
SQL

echo "==> Start redo apply"
docker exec -i "$STANDBY" bash -lc "$ENVSET; sqlplus -s / as sysdba" <<SQL
ALTER DATABASE RECOVER MANAGED STANDBY DATABASE DISCONNECT FROM SESSION;
EXIT;
SQL

sleep 30

docker exec -i "$STANDBY" bash -lc "$ENVSET; sqlplus -s / as sysdba" <<SQL
SET LINESIZE 180
SELECT process, status, sequence# FROM v\$managed_standby
WHERE process IN ('MRP0','RFS') ORDER BY process;
SELECT recovery_mode FROM v\$archive_dest_status WHERE dest_id = 1;
SELECT COUNT(*) AS srl_count FROM v\$standby_log;
EXIT;
SQL

echo "==> Re-enable transport on primary"
docker exec -i "$PRIMARY" bash -lc "$ENVSET; sqlplus -s / as sysdba" <<SQL
ALTER SYSTEM SET log_archive_dest_state_2='DEFER' SCOPE=BOTH;
ALTER SYSTEM SET log_archive_dest_state_2='ENABLE' SCOPE=BOTH;
ALTER SYSTEM SWITCH LOGFILE;
SELECT dest_id, status, gap_status, error FROM v\$archive_dest_status WHERE dest_id = 2;
EXIT;
SQL

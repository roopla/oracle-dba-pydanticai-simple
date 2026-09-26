#!/usr/bin/env bash
# ---------------------------------------------------------------------
# 03-duplicate.sh  (revision 2)
#
# Builds the standby with RMAN DUPLICATE FROM ACTIVE DATABASE, then
# starts redo apply and verifies the configuration.
#
# The SPFILE ... SET block has been REMOVED. Those parameters are already
# in the standby spfile from the first attempt, and it was the
# "shutdown clone immediate" that the SPFILE clause forces which hit
# ORA-00600 [ksb_shut_detached_process3] on SMCO. With no SPFILE clause,
# RMAN goes straight to restoring datafiles.
#
# Prerequisites: standby up in NOMOUNT from its spfile, db_unique_name
# showing ORCLCDB_STBY, and the standby listener running.
#
# This copies every datafile across the network. On a busy host it can
# take a while and generates heavy I/O - run it when nothing else needs
# the box.
#
# Usage:
#     export SYS_PASSWORD='your_sys_password'
#     ./03-duplicate.sh
# ---------------------------------------------------------------------

set -euo pipefail

PRIMARY_CONTAINER="oracle19c"
STANDBY_CONTAINER="oracle19c-stby"
ORACLE_SID="ORCLCDB"
STANDBY_UNIQUE="ORCLCDB_STBY"
ORACLE_HOME="/opt/oracle/product/19c/dbhome_1"
STANDBY_SGA="1G"
STANDBY_PGA="256M"

: "${SYS_PASSWORD:?Set SYS_PASSWORD before running}"

ENV="export ORACLE_HOME=${ORACLE_HOME} ORACLE_SID=${ORACLE_SID} \
     PATH=${ORACLE_HOME}/bin:\$PATH"

echo "==> Pre-flight: auxiliary must be NOMOUNT with db_unique_name=${STANDBY_UNIQUE}"
docker exec "${STANDBY_CONTAINER}" bash -lc "${ENV}; \
sqlplus -s / as sysdba <<'EOF'
SET LINESIZE 150
SELECT instance_name, status FROM v\$instance;
SHOW PARAMETER db_unique_name
EOF"

echo
read -r -p "Does the above show STARTED and ORCLCDB_STBY? [y/N] " ok
[[ "${ok}" == "y" || "${ok}" == "Y" ]] || { echo "Aborting."; exit 1; }

echo "==> Running RMAN DUPLICATE (this is the long step)"
docker exec "${STANDBY_CONTAINER}" bash -lc "${ENV}; \
rman target sys/'${SYS_PASSWORD}'@ORCLCDB auxiliary sys/'${SYS_PASSWORD}'@${STANDBY_UNIQUE} <<'EOF'
RUN {
  ALLOCATE CHANNEL prm1 TYPE DISK;
  ALLOCATE AUXILIARY CHANNEL aux1 TYPE DISK;

  DUPLICATE TARGET DATABASE FOR STANDBY
    FROM ACTIVE DATABASE
    DORECOVER
    NOFILENAMECHECK;
}
EXIT;
EOF"

echo "==> Starting redo apply"
docker exec "${STANDBY_CONTAINER}" bash -lc "${ENV}; \
sqlplus -s / as sysdba <<'EOF'
ALTER DATABASE RECOVER MANAGED STANDBY DATABASE
  USING CURRENT LOGFILE DISCONNECT FROM SESSION;
EXIT;
EOF"

echo "==> Waiting 30s for apply to settle"
sleep 30

echo
echo "===================== STANDBY ====================="
docker exec "${STANDBY_CONTAINER}" bash -lc "${ENV}; \
sqlplus -s / as sysdba <<'EOF'
SET LINESIZE 200 PAGESIZE 100
COLUMN name FORMAT A22
COLUMN value FORMAT A22
SELECT name, db_unique_name, database_role, open_mode, protection_mode
FROM   v\\\$database;
PROMPT
PROMPT -- Lag (should be small and non-null) --
SELECT name, value, unit FROM v\\\$dataguard_stats
WHERE  name IN ('transport lag','apply lag');
PROMPT
PROMPT -- Apply processes (MRP0 must be present) --
SELECT process, status, thread#, sequence# FROM v\\\$managed_standby
WHERE  process IN ('MRP0','RFS');
EXIT;
EOF"

echo
echo "===================== PRIMARY ====================="
docker exec "${PRIMARY_CONTAINER}" bash -lc "${ENV}; \
sqlplus -s / as sysdba <<'EOF'
SET LINESIZE 200 PAGESIZE 100
COLUMN dest_name FORMAT A24
COLUMN error     FORMAT A30
SELECT dest_id, dest_name, status, gap_status, error
FROM   v\\\$archive_dest_status
WHERE  status <> 'INACTIVE';
EXIT;
EOF"

echo
echo "Done. MRP0 present with small lag and no destination error means"
echo "the standby is applying. Next: wire it into the agent (see README)."

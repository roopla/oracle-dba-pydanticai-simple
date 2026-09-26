#!/usr/bin/env bash
# ---------------------------------------------------------------------
# 03-duplicate.sh  (revision 3)
#
# Builds the standby with RMAN DUPLICATE FROM ACTIVE DATABASE, then
# starts redo apply and verifies the configuration.
#
# No SPFILE ... SET clause: 02-create-standby.sh already started the
# auxiliary from a complete standby spfile. The SPFILE clause forces a
# "shutdown clone immediate", which previously hit ORA-00600
# [ksb_shut_detached_process3] on SMCO.
#
# Credentials go to RMAN on stdin, never on its command line, so they
# do not appear in `ps`. The password is quoted because RMAN treats an
# unquoted '#' as the start of a comment.
#
# Prerequisites: standby up in NOMOUNT with db_unique_name ORCL_STBY
# and its listener running (02-create-standby.sh).
#
# This copies every datafile across the Docker network. It can take a
# while and generates heavy I/O - run it when nothing else needs the box.
#
# Usage:
#     export SYS_PASSWORD='your_sys_password'
#     ./03-duplicate.sh              # asks for confirmation
#     ASSUME_YES=1 ./03-duplicate.sh # non-interactive
# ---------------------------------------------------------------------

set -euo pipefail

PRIMARY_CONTAINER="oracle19c"
STANDBY_CONTAINER="oracle19c-stby"
ORACLE_SID="ORCL"
PRIMARY_UNIQUE="ORCL"
STANDBY_UNIQUE="ORCL_STBY"
ORACLE_HOME="/opt/oracle/product/19c/dbhome_1"

: "${SYS_PASSWORD:?Set SYS_PASSWORD before running}"

ENV="export ORACLE_HOME=${ORACLE_HOME} ORACLE_SID=${ORACLE_SID} \
     PATH=${ORACLE_HOME}/bin:\$PATH"

sb()  { docker exec -i -u oracle "${STANDBY_CONTAINER}" bash -lc "${ENV}; $1"; }
pri() { docker exec -i "${PRIMARY_CONTAINER}" bash -lc "$1"; }

echo "==> Pre-flight: auxiliary must be NOMOUNT (STARTED) as ${STANDBY_UNIQUE}"
preflight=$(sb "sqlplus -s / as sysdba" <<'EOF'
SET HEADING OFF FEEDBACK OFF PAGESIZE 0
SELECT status || ' ' || (SELECT value FROM v$parameter
                         WHERE name = 'db_unique_name')
FROM v$instance;
EXIT;
EOF
)
preflight=$(echo "${preflight}" | xargs)
echo "    ${preflight}"

if [[ "${preflight}" != "STARTED ${STANDBY_UNIQUE}" ]]; then
  echo "Expected 'STARTED ${STANDBY_UNIQUE}'. Run 02-create-standby.sh first."
  exit 1
fi

if [[ "${ASSUME_YES:-0}" != "1" ]]; then
  read -r -p "Start RMAN duplicate? [y/N] " ok
  [[ "${ok}" == "y" || "${ok}" == "Y" ]] || { echo "Aborting."; exit 1; }
fi

echo "==> Running RMAN DUPLICATE (this is the long step)"
sb "rman" <<EOF
CONNECT TARGET 'sys/"${SYS_PASSWORD}"@${PRIMARY_UNIQUE}';
CONNECT AUXILIARY 'sys/"${SYS_PASSWORD}"@${STANDBY_UNIQUE}';
RUN {
  ALLOCATE CHANNEL prm1 TYPE DISK;
  ALLOCATE CHANNEL prm2 TYPE DISK;
  ALLOCATE AUXILIARY CHANNEL aux1 TYPE DISK;
  ALLOCATE AUXILIARY CHANNEL aux2 TYPE DISK;

  DUPLICATE TARGET DATABASE FOR STANDBY
    FROM ACTIVE DATABASE
    DORECOVER
    NOFILENAMECHECK;
}
EXIT;
EOF

echo "==> Starting redo apply"
sb "sqlplus -s / as sysdba" <<'EOF'
WHENEVER SQLERROR EXIT FAILURE
ALTER DATABASE RECOVER MANAGED STANDBY DATABASE DISCONNECT FROM SESSION;
EXIT;
EOF

echo "==> Forcing a log switch on the primary so redo ships immediately"
pri "sqlplus -s / as sysdba" <<'EOF'
ALTER SYSTEM ARCHIVE LOG CURRENT;
EXIT;
EOF

echo "==> Waiting 30s for apply to settle"
sleep 30

echo
echo "===================== STANDBY ====================="
sb "sqlplus -s / as sysdba" <<'EOF'
SET LINESIZE 200 PAGESIZE 100
COLUMN name  FORMAT A22
COLUMN value FORMAT A22
SELECT name, db_unique_name, database_role, open_mode, protection_mode
FROM   v$database;
PROMPT
PROMPT -- Lag (should be small and non-null) --
SELECT name, value, unit FROM v$dataguard_stats
WHERE  name IN ('transport lag','apply lag');
PROMPT
PROMPT -- Apply processes (MRP0 must be present) --
SELECT process, status, thread#, sequence# FROM v$managed_standby
WHERE  process IN ('MRP0','RFS');
EXIT;
EOF

echo
echo "===================== PRIMARY ====================="
pri "sqlplus -s / as sysdba" <<'EOF'
SET LINESIZE 200 PAGESIZE 100
COLUMN dest_name FORMAT A24
COLUMN error     FORMAT A40
SELECT dest_id, dest_name, status, gap_status, error
FROM   v$archive_dest_status
WHERE  status <> 'INACTIVE';
EXIT;
EOF

echo
echo "Done. MRP0 present with small lag and no destination error means"
echo "the standby is applying. Next: wire it into the agent (see README)."

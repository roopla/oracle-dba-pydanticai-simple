#!/usr/bin/env bash
# ---------------------------------------------------------------------
# 02-create-standby.sh
#
# Creates the standby container, its listener and tnsnames, copies the
# password file across, and starts the auxiliary instance in NOMOUNT
# from a complete standby spfile.
#
# It does NOT create a database - the standby is built by RMAN in step 3.
# That is why the container is started with a plain bash entrypoint
# instead of the image's normal one, which would run DBCA.
#
# All configuration (spfile, password file, listener.ora, tnsnames.ora)
# is kept on the standby VOLUME under dbconfig/ORCL and symlinked into
# $ORACLE_HOME, the same layout the stock image uses on the primary.
# $ORACLE_HOME lives in the image, so anything written only there is
# lost when the container is recreated. Run link-standby-config.sh
# after recreating the container to restore the symlinks.
#
# Refuses to replace an existing standby container unless REBUILD=1.
#
# Usage:
#     ./02-create-standby.sh
# ---------------------------------------------------------------------

set -euo pipefail

PRIMARY_CONTAINER="oracle19c"
STANDBY_CONTAINER="oracle19c-stby"
STANDBY_VOLUME="oracle19c-stby-data"
NETWORK="oracle-dg"
STANDBY_IP="172.23.0.3"              # hardcoded in ORACLE_STANDBY_DSN
IMAGE="container-registry.oracle.com/database/enterprise:19.3.0.0"

ORACLE_SID="ORCL"                    # db_name - same on both sides
PRIMARY_UNIQUE="ORCL"                # db_unique_name of the primary
STANDBY_UNIQUE="ORCL_STBY"           # db_unique_name - must differ
ORACLE_HOME="/opt/oracle/product/19c/dbhome_1"
DATA="/opt/oracle/oradata"
DBCONFIG="${DATA}/dbconfig/${ORACLE_SID}"
HOST_PORT="1522"                     # 1521 = FREE, 1523 = primary

# A mounted standby applying redo needs far less than the primary's
# 2.3G SGA.
STANDBY_SGA="1536M"
STANDBY_PGA="512M"

sb() { docker exec -i -u oracle "${STANDBY_CONTAINER}" bash -lc "$1"; }
ENV="export ORACLE_HOME=${ORACLE_HOME} ORACLE_SID=${ORACLE_SID} \
     PATH=${ORACLE_HOME}/bin:\$PATH"

echo "==> Checking primary is up"
docker exec "${PRIMARY_CONTAINER}" true

if docker container inspect "${STANDBY_CONTAINER}" >/dev/null 2>&1; then
  if [[ "${REBUILD:-0}" != "1" ]]; then
    echo "${STANDBY_CONTAINER} already exists. To throw it away and start"
    echo "again, remove it and its volume, or re-run with REBUILD=1."
    exit 1
  fi
  echo "==> REBUILD=1: removing ${STANDBY_CONTAINER} and ${STANDBY_VOLUME}"
  docker rm -f "${STANDBY_CONTAINER}" >/dev/null
  docker volume rm "${STANDBY_VOLUME}" >/dev/null 2>&1 || true
fi

echo "==> Creating volume ${STANDBY_VOLUME}"
docker volume create "${STANDBY_VOLUME}" >/dev/null

# --init: tini as PID 1 reaps exited MRP0 processes. Without it they
# accumulate as zombies and restarting apply fails with ORA-16448.
echo "==> Starting ${STANDBY_CONTAINER} (no DBCA)"
docker run -d \
  --name "${STANDBY_CONTAINER}" \
  --hostname "${STANDBY_CONTAINER}" \
  --init \
  --network "${NETWORK}" \
  --ip "${STANDBY_IP}" \
  -p "${HOST_PORT}:1521" \
  -e ORACLE_SID="${ORACLE_SID}" \
  -e ORACLE_BASE=/opt/oracle \
  -e ORACLE_HOME="${ORACLE_HOME}" \
  -v "${STANDBY_VOLUME}:${DATA}" \
  --entrypoint /bin/bash \
  "${IMAGE}" -c 'tail -f /dev/null' >/dev/null

echo "==> Creating directories on standby"
docker exec -u 0 "${STANDBY_CONTAINER}" bash -c "
  mkdir -p ${DATA}/${ORACLE_SID}/archive_logs \
           ${DATA}/${ORACLE_SID}/ORCLPDB1 \
           ${DATA}/${ORACLE_SID}/pdbseed \
           ${DBCONFIG} \
           /opt/oracle/admin/${ORACLE_SID}/adump
  chown -R oracle:oinstall ${DATA} /opt/oracle/admin
"

echo "==> Copying password file from primary"
docker exec "${PRIMARY_CONTAINER}" cat "${ORACLE_HOME}/dbs/orapw${ORACLE_SID}" \
  | sb "cat > ${DBCONFIG}/orapw${ORACLE_SID} && chmod 640 ${DBCONFIG}/orapw${ORACLE_SID}"

echo "==> Writing standby listener.ora (static entry required for NOMOUNT)"
sb "cat > ${DBCONFIG}/listener.ora" <<EOF
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
      (GLOBAL_DBNAME = ${STANDBY_UNIQUE})
      (ORACLE_HOME = ${ORACLE_HOME})
      (SID_NAME = ${ORACLE_SID})
    )
    (SID_DESC =
      (GLOBAL_DBNAME = ${STANDBY_UNIQUE}_DGMGRL)
      (ORACLE_HOME = ${ORACLE_HOME})
      (SID_NAME = ${ORACLE_SID})
    )
  )
EOF

echo "==> Writing tnsnames.ora on both containers"
# The primary's tnsnames.ora is the stock image's symlink into its own
# dbconfig, so writing through it persists on the primary volume too.
TNS=$(cat <<EOF
${PRIMARY_UNIQUE} =
  (DESCRIPTION =
    (ADDRESS = (PROTOCOL = TCP)(HOST = ${PRIMARY_CONTAINER})(PORT = 1521))
    (CONNECT_DATA = (SERVER = DEDICATED)(SERVICE_NAME = ${PRIMARY_UNIQUE}))
  )

${STANDBY_UNIQUE} =
  (DESCRIPTION =
    (ADDRESS = (PROTOCOL = TCP)(HOST = ${STANDBY_CONTAINER})(PORT = 1521))
    (CONNECT_DATA = (SERVER = DEDICATED)(SERVICE_NAME = ${STANDBY_UNIQUE})(UR = A))
  )

ORCLPDB1 =
  (DESCRIPTION =
    (ADDRESS = (PROTOCOL = TCP)(HOST = ${PRIMARY_CONTAINER})(PORT = 1521))
    (CONNECT_DATA = (SERVER = DEDICATED)(SERVICE_NAME = ORCLPDB1))
  )
EOF
)
echo "${TNS}" | docker exec -i "${PRIMARY_CONTAINER}" bash -c \
  "cat > ${ORACLE_HOME}/network/admin/tnsnames.ora"
echo "${TNS}" | sb "cat > ${DBCONFIG}/tnsnames.ora"

echo "==> Writing standby pfile"
# Mirrors the primary (compatible, block size, control file path) with
# the standby's own db_unique_name and reversed redo transport, so the
# standby is also ready to become primary after a switchover.
sb "cat > ${DBCONFIG}/init${ORACLE_SID}.ora" <<EOF
db_name='${ORACLE_SID}'
db_unique_name='${STANDBY_UNIQUE}'
compatible='19.0.0'
db_block_size=8192
control_files='${DATA}/${ORACLE_SID}/control01.ctl'
sga_target=${STANDBY_SGA}
pga_aggregate_target=${STANDBY_PGA}
processes=320
open_cursors=300
undo_tablespace='UNDOTBS1'
audit_file_dest='/opt/oracle/admin/${ORACLE_SID}/adump'
diagnostic_dest='/opt/oracle'
enable_pluggable_database=TRUE
remote_login_passwordfile='EXCLUSIVE'
log_archive_config='DG_CONFIG=(${PRIMARY_UNIQUE},${STANDBY_UNIQUE})'
log_archive_dest_1='LOCATION=${DATA}/${ORACLE_SID}/archive_logs VALID_FOR=(ALL_LOGFILES,ALL_ROLES) DB_UNIQUE_NAME=${STANDBY_UNIQUE}'
log_archive_dest_2='SERVICE=${PRIMARY_UNIQUE} ASYNC NOAFFIRM VALID_FOR=(ONLINE_LOGFILES,PRIMARY_ROLE) DB_UNIQUE_NAME=${PRIMARY_UNIQUE}'
log_archive_dest_state_2='ENABLE'
fal_server='${PRIMARY_UNIQUE}'
standby_file_management='AUTO'
EOF

echo "==> Linking config into \$ORACLE_HOME"
docker exec -i -u oracle "${STANDBY_CONTAINER}" bash -s <<EOF
set -e
ln -sf ${DBCONFIG}/orapw${ORACLE_SID}      ${ORACLE_HOME}/dbs/orapw${ORACLE_SID}
ln -sf ${DBCONFIG}/spfile${ORACLE_SID}.ora ${ORACLE_HOME}/dbs/spfile${ORACLE_SID}.ora
ln -sf ${DBCONFIG}/listener.ora            ${ORACLE_HOME}/network/admin/listener.ora
ln -sf ${DBCONFIG}/tnsnames.ora            ${ORACLE_HOME}/network/admin/tnsnames.ora
EOF

echo "==> Starting standby listener"
sb "${ENV}; lsnrctl start >/dev/null && lsnrctl status | grep -i service"

echo "==> Creating spfile and starting auxiliary instance in NOMOUNT"
sb "${ENV}; sqlplus -s / as sysdba" <<EOF
WHENEVER SQLERROR EXIT FAILURE
CREATE SPFILE='${DBCONFIG}/spfile${ORACLE_SID}.ora'
  FROM PFILE='${DBCONFIG}/init${ORACLE_SID}.ora';
STARTUP NOMOUNT;
SELECT instance_name, status FROM v\$instance;
SHOW PARAMETER db_unique_name
EXIT;
EOF

echo "==> Verifying connectivity in both directions"
docker exec "${PRIMARY_CONTAINER}" bash -lc "tnsping ${STANDBY_UNIQUE} | tail -1"
sb "${ENV}; tnsping ${PRIMARY_UNIQUE} | tail -1"

echo
echo "Standby container ready in NOMOUNT. Next: ./03-duplicate.sh"

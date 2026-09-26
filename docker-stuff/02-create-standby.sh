#!/usr/bin/env bash
# ---------------------------------------------------------------------
# 02-create-standby.sh
#
# Creates the standby container, its listener and tnsnames, copies the
# password file across, and starts the auxiliary instance in NOMOUNT.
#
# It does NOT create a database - the standby is built by RMAN in step 3.
# That is why the container is started with a plain bash entrypoint
# instead of the image's normal one, which would run DBCA.
#
# Usage:
#     export SYS_PASSWORD='your_sys_password'
#     ./02-create-standby.sh
# ---------------------------------------------------------------------

set -euo pipefail

PRIMARY_CONTAINER="oracle19c"
STANDBY_CONTAINER="oracle19c-stby"
STANDBY_VOLUME="oracle19c-stby-data"
NETWORK="oracle-demo"
IMAGE="oracle/database:19.3.0-ee-ext"

ORACLE_SID="ORCLCDB"                 # db_name - same on both sides
STANDBY_UNIQUE="ORCLCDB_STBY"        # db_unique_name - must differ
ORACLE_HOME="/opt/oracle/product/19c/dbhome_1"
HOST_PORT="1522"                     # primary already owns 1521

# Keep the standby small. A mounted standby applying redo does not need
# a large buffer cache, and this host is memory-constrained.
STANDBY_SGA="1G"
STANDBY_PGA="256M"

: "${SYS_PASSWORD:?Set SYS_PASSWORD before running}"

echo "==> Checking primary is up"
docker exec "${PRIMARY_CONTAINER}" bash -lc 'echo ok' >/dev/null

echo "==> Creating volume ${STANDBY_VOLUME}"
docker volume create "${STANDBY_VOLUME}" >/dev/null

echo "==> Starting ${STANDBY_CONTAINER} (no DBCA)"
docker rm -f "${STANDBY_CONTAINER}" 2>/dev/null || true
docker run -d \
  --name "${STANDBY_CONTAINER}" \
  --hostname "${STANDBY_CONTAINER}" \
  --network "${NETWORK}" \
  -p "${HOST_PORT}:1521" \
  -e ORACLE_SID="${ORACLE_SID}" \
  -e ORACLE_BASE=/opt/oracle \
  -e ORACLE_HOME="${ORACLE_HOME}" \
  -v "${STANDBY_VOLUME}:/opt/oracle/oradata" \
  --entrypoint /bin/bash \
  "${IMAGE}" -c 'tail -f /dev/null' >/dev/null

# Make sure the primary can also reach the standby by name.
docker network connect "${NETWORK}" "${PRIMARY_CONTAINER}" 2>/dev/null || true

echo "==> Creating directories on standby"
docker exec "${STANDBY_CONTAINER}" bash -lc "
  mkdir -p /opt/oracle/oradata/${ORACLE_SID} \
           /opt/oracle/oradata/${ORACLE_SID}/pdbseed \
           /opt/oracle/admin/${ORACLE_SID}/adump \
           /opt/oracle/fast_recovery_area/${ORACLE_SID} \
           /opt/oracle/oradata/dbconfig
"

echo "==> Copying password file from primary"
docker exec "${PRIMARY_CONTAINER}" bash -lc \
  "cat ${ORACLE_HOME}/dbs/orapw${ORACLE_SID}" > /tmp/orapw_copy
docker cp /tmp/orapw_copy "${STANDBY_CONTAINER}:${ORACLE_HOME}/dbs/orapw${ORACLE_SID}"
rm -f /tmp/orapw_copy
docker exec -u 0 "${STANDBY_CONTAINER}" bash -lc \
  "chown oracle:oinstall ${ORACLE_HOME}/dbs/orapw${ORACLE_SID} && \
   chmod 640 ${ORACLE_HOME}/dbs/orapw${ORACLE_SID}"

echo "==> Writing standby listener.ora (static entry required for NOMOUNT)"
docker exec "${STANDBY_CONTAINER}" bash -lc "cat > ${ORACLE_HOME}/network/admin/listener.ora <<'EOF'
LISTENER =
  (DESCRIPTION_LIST =
    (DESCRIPTION =
      (ADDRESS = (PROTOCOL = TCP)(HOST = ${STANDBY_CONTAINER})(PORT = 1521))
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
EOF"

echo "==> Writing tnsnames.ora on both containers"
TNS=$(cat <<EOF
ORCLCDB =
  (DESCRIPTION =
    (ADDRESS = (PROTOCOL = TCP)(HOST = ${PRIMARY_CONTAINER})(PORT = 1521))
    (CONNECT_DATA = (SERVER = DEDICATED)(SERVICE_NAME = ${ORACLE_SID}.localdomain))
  )

${STANDBY_UNIQUE} =
  (DESCRIPTION =
    (ADDRESS = (PROTOCOL = TCP)(HOST = ${STANDBY_CONTAINER})(PORT = 1521))
    (CONNECT_DATA = (SERVER = DEDICATED)(SERVICE_NAME = ${STANDBY_UNIQUE}))
  )
EOF
)

for c in "${PRIMARY_CONTAINER}" "${STANDBY_CONTAINER}"; do
  docker exec "$c" bash -lc "cat > ${ORACLE_HOME}/network/admin/tnsnames.ora <<'EOF'
${TNS}
EOF"
done

echo "==> Starting standby listener"
docker exec "${STANDBY_CONTAINER}" bash -lc \
  "export ORACLE_HOME=${ORACLE_HOME} ORACLE_SID=${ORACLE_SID} \
     PATH=${ORACLE_HOME}/bin:\$PATH; lsnrctl start" || true

echo "==> Writing minimal init pfile for the auxiliary instance"
docker exec "${STANDBY_CONTAINER}" bash -lc "cat > ${ORACLE_HOME}/dbs/init${ORACLE_SID}.ora <<EOF
db_name='${ORACLE_SID}'
db_unique_name='${STANDBY_UNIQUE}'
sga_target=${STANDBY_SGA}
pga_aggregate_target=${STANDBY_PGA}
db_block_size=8192
audit_file_dest='/opt/oracle/admin/${ORACLE_SID}/adump'
enable_pluggable_database=TRUE
EOF"

echo "==> Starting auxiliary instance in NOMOUNT"
docker exec "${STANDBY_CONTAINER}" bash -lc \
  "export ORACLE_HOME=${ORACLE_HOME} ORACLE_SID=${ORACLE_SID} \
     PATH=${ORACLE_HOME}/bin:\$PATH; \
   sqlplus -s / as sysdba <<'EOF'
STARTUP NOMOUNT PFILE='${ORACLE_HOME}/dbs/init${ORACLE_SID}.ora';
SELECT instance_name, status FROM v\\\$instance;
EXIT;
EOF"

echo "==> Verifying connectivity in both directions"
docker exec "${PRIMARY_CONTAINER}" bash -lc \
  "export ORACLE_HOME=${ORACLE_HOME} PATH=${ORACLE_HOME}/bin:\$PATH; \
   tnsping ${STANDBY_UNIQUE} | tail -2"
docker exec "${STANDBY_CONTAINER}" bash -lc \
  "export ORACLE_HOME=${ORACLE_HOME} PATH=${ORACLE_HOME}/bin:\$PATH; \
   tnsping ORCLCDB | tail -2"

echo
echo "Standby container ready in NOMOUNT. Next: ./03-duplicate.sh"

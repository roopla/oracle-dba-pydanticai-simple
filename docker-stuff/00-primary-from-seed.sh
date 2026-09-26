#!/usr/bin/env bash
# ---------------------------------------------------------------------
# 00-primary-from-seed.sh
#
# Creates the ORCL primary container from the pre-built seed image
# (oracle-dg-seed:v1: a cleanly shut down ORCL / ORCLPDB1 database with
# ARCHIVELOG and FORCE LOGGING already on). Minutes instead of a
# 30-40 minute DBCA run.
#
# The stock 19c image sees existing datafiles under
# /opt/oracle/oradata/ORCL and opens them instead of running DBCA.
#
# Safe to re-run: an existing network, volume or container is reused.
#
# Usage:
#     ./00-primary-from-seed.sh
# ---------------------------------------------------------------------

set -euo pipefail

NETWORK="oracle-dg"
SUBNET="172.23.0.0/16"

PRIMARY_CONTAINER="oracle19c"
PRIMARY_IP="172.23.0.2"             # standby is 172.23.0.3
PRIMARY_VOLUME="oracle19c-data"
HOST_PORT="1523"                    # 1521 = FREE, 1522 = standby

IMAGE="container-registry.oracle.com/database/enterprise:19.3.0.0"
SEED_IMAGE="oracle-dg-seed:v1"

ORACLE_SID="ORCL"
ORACLE_PDB="ORCLPDB1"

READY_TIMEOUT_SECONDS=600

echo "==> Network ${NETWORK} (${SUBNET})"
docker network inspect "${NETWORK}" >/dev/null 2>&1 \
  || docker network create --subnet "${SUBNET}" "${NETWORK}" >/dev/null

if docker container inspect "${PRIMARY_CONTAINER}" >/dev/null 2>&1; then
  echo "==> ${PRIMARY_CONTAINER} already exists - starting it"
  docker start "${PRIMARY_CONTAINER}" >/dev/null
else
  echo "==> Volume ${PRIMARY_VOLUME}"
  docker volume create "${PRIMARY_VOLUME}" >/dev/null

  # Only restore into an empty volume, never over a live database.
  if docker run --rm -v "${PRIMARY_VOLUME}:/data" alpine \
       test -d "/data/${ORACLE_SID}"; then
    echo "==> Volume already holds ${ORACLE_SID} - skipping seed restore"
  else
    # The stock image opens an existing database only when the checkpoint
    # file oradata/.<SID>.created is present (runOracle.sh). The seed tar
    # does not carry it, and without it the image runs DBCA over the seed.
    echo "==> Restoring seed into ${PRIMARY_VOLUME} (3.8G, a few minutes)"
    docker run --rm -v "${PRIMARY_VOLUME}:/opt/oracle/oradata" \
      "${SEED_IMAGE}" sh -c \
      "tar -xf /seed/orcl-seed.tar -C /opt/oracle/oradata \
       && mkdir -p /opt/oracle/oradata/fast_recovery_area \
       && touch /opt/oracle/oradata/.${ORACLE_SID}.created \
       && chown -R 54321:54321 /opt/oracle/oradata"
  fi

  # --init: tini as PID 1 reaps exited Oracle background processes.
  # Without it they linger as zombies (see RUNBOOK, ORA-16448).
  echo "==> Starting ${PRIMARY_CONTAINER}"
  docker run -d \
    --name "${PRIMARY_CONTAINER}" \
    --hostname "${PRIMARY_CONTAINER}" \
    --init \
    --network "${NETWORK}" \
    --ip "${PRIMARY_IP}" \
    -p "${HOST_PORT}:1521" \
    -e ORACLE_SID="${ORACLE_SID}" \
    -e ORACLE_PDB="${ORACLE_PDB}" \
    -v "${PRIMARY_VOLUME}:/opt/oracle/oradata" \
    "${IMAGE}" >/dev/null
fi

echo "==> Waiting for the database (up to ${READY_TIMEOUT_SECONDS}s)"
deadline=$((SECONDS + READY_TIMEOUT_SECONDS))
until docker logs "${PRIMARY_CONTAINER}" 2>&1 | grep -q "DATABASE IS READY TO USE"; do
  if (( SECONDS > deadline )); then
    echo "Timed out. Last log lines:"
    docker logs --tail 30 "${PRIMARY_CONTAINER}"
    exit 1
  fi
  sleep 10
done

docker exec -i "${PRIMARY_CONTAINER}" bash -lc 'sqlplus -s / as sysdba' <<'EOF'
SET LINESIZE 200 PAGESIZE 100
COLUMN name FORMAT A12
SELECT name, db_unique_name, log_mode, force_logging, open_mode,
       database_role
FROM   v$database;
SELECT name, open_mode FROM v$pdbs;
EXIT;
EOF

echo
echo "Primary ready. Next: 01-primary-prep.sql"

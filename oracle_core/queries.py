"""Plain Python functions that retrieve Oracle DBA information."""

from typing import Any

from oracle_core.config import get_settings
from oracle_core.db import query


PROTECTED_PDB_NAMES = {
    "PDB$SEED",
}


def list_pdbs() -> list[dict[str, Any]]:
    """Return all pluggable databases visible from CDB$ROOT."""
    settings = get_settings()

    return query(
        """
        SELECT
            name,
            open_mode,
            restricted
        FROM v$pdbs
        ORDER BY name
        """,
        database_name=settings.oracle_cdb_name,
    )


def normalize_pdb_name(pdb_name: str) -> str:
    """Normalize a user-supplied PDB name."""
    normalized_name = pdb_name.strip().upper()

    if not normalized_name:
        raise ValueError("PDB name must not be empty")

    return normalized_name


def validate_pdb_name(pdb_name: str) -> str:
    """Validate a PDB name and return its normalized value.

    The name is checked against the live PDB list from CDB$ROOT.
    PDB$SEED is protected from normal application queries.
    """
    normalized_name = normalize_pdb_name(pdb_name)

    if normalized_name in PROTECTED_PDB_NAMES:
        raise ValueError(
            f"{normalized_name} is a protected template PDB and "
            "cannot be used for normal database queries"
        )

    pdb_rows = list_pdbs()

    available_pdb_names = {
        str(row["name"]).strip().upper()
        for row in pdb_rows
    }

    if normalized_name not in available_pdb_names:
        selectable_pdb_names = sorted(
            available_pdb_names - PROTECTED_PDB_NAMES
        )

        available_text = (
            ", ".join(selectable_pdb_names)
            if selectable_pdb_names
            else "none"
        )

        raise ValueError(
            f"Unknown PDB '{normalized_name}'. "
            f"Available PDBs: {available_text}"
        )

    return normalized_name


def get_database_version(
    pdb_name: str | None = None,
) -> list[dict[str, Any]]:
    """Return the Oracle database version for a validated PDB."""
    settings = get_settings()

    requested_pdb_name = (
        pdb_name or settings.oracle_default_pdb_name
    )

    validated_pdb_name = validate_pdb_name(
        requested_pdb_name
    )

    return query(
        """
        SELECT banner
        FROM v$version
        WHERE banner LIKE 'Oracle%Database%'
        """,
        database_name=validated_pdb_name,
    )


def get_database_identity() -> list[dict[str, Any]]:
    """Return identifying information for the container database."""
    settings = get_settings()

    return query(
        """
        SELECT
            name,
            db_unique_name,
            dbid,
            cdb
        FROM v$database
        """,
        database_name=settings.oracle_cdb_name,
    )


def get_instance_status() -> list[dict[str, Any]]:
    """Return the current Oracle instance status."""
    settings = get_settings()

    return query(
        """
        SELECT
            instance_name,
            host_name,
            version,
            status,
            database_status,
            instance_role
        FROM v$instance
        """,
        database_name=settings.oracle_cdb_name,
    )


def get_database_role() -> list[dict[str, Any]]:
    """Return the database role and Data Guard-related state."""
    settings = get_settings()

    return query(
        """
        SELECT
            name,
            db_unique_name,
            database_role,
            open_mode,
            protection_mode,
            protection_level,
            switchover_status
        FROM v$database
        """,
        database_name=settings.oracle_cdb_name,
    )


def get_database_startup_time() -> list[dict[str, Any]]:
    """Return when the Oracle instance was last started."""
    settings = get_settings()

    return query(
        """
        SELECT
            instance_name,
            startup_time
        FROM v$instance
        """,
        database_name=settings.oracle_cdb_name,
    )


def get_pdb_status(
    pdb_name: str | None = None,
) -> list[dict[str, Any]]:
    """Return the status of one validated pluggable database."""
    settings = get_settings()

    requested_pdb_name = (
        pdb_name or settings.oracle_default_pdb_name
    )
    validated_pdb_name = validate_pdb_name(
        requested_pdb_name
    )

    return query(
        """
        SELECT
            con_id,
            name,
            open_mode,
            restricted
        FROM v$pdbs
        WHERE UPPER(name) = :pdb_name
        """,
        database_name=settings.oracle_cdb_name,
        binds={
            "pdb_name": validated_pdb_name,
        },
    )



def get_active_sessions(
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return currently active user sessions across the CDB."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")

    settings = get_settings()

    return query(
        """
        SELECT *
        FROM (
            SELECT
                s.sid,
                s.serial# AS serial_num,
                s.username,
                s.status,
                s.sql_id,
                s.event,
                s.wait_class,
                s.seconds_in_wait,
                s.last_call_et AS seconds_active,
                s.module,
                s.action,
                s.machine,
                c.name AS con_name
            FROM v$session s
            LEFT JOIN v$containers c
              ON c.con_id = s.con_id
            WHERE s.status = 'ACTIVE'
              AND s.type = 'USER'
              AND s.username IS NOT NULL
            ORDER BY s.last_call_et DESC
        )
        WHERE ROWNUM <= :limit
        """,
        database_name=settings.oracle_cdb_name,
        binds={"limit": limit},
    )


def get_blocking_sessions(
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return sessions that are currently blocked by another session."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")

    settings = get_settings()

    return query(
        """
        SELECT *
        FROM (
            SELECT
                blocker.sid AS blocking_sid,
                blocker.serial# AS blocking_serial,
                blocker.username AS blocking_user,
                blocker.sql_id AS blocking_sql_id,
                blocker.module AS blocking_module,
                waiter.sid AS waiting_sid,
                waiter.serial# AS waiting_serial,
                waiter.username AS waiting_user,
                waiter.sql_id AS waiting_sql_id,
                waiter.event AS waiting_event,
                waiter.wait_class AS waiting_wait_class,
                waiter.seconds_in_wait AS waiting_seconds,
                waiter.module AS waiting_module,
                c.name AS con_name
            FROM v$session waiter
            JOIN v$session blocker
              ON blocker.sid = waiter.blocking_session
            LEFT JOIN v$containers c
              ON c.con_id = waiter.con_id
            WHERE waiter.blocking_session IS NOT NULL
            ORDER BY waiter.seconds_in_wait DESC
        )
        WHERE ROWNUM <= :limit
        """,
        database_name=settings.oracle_cdb_name,
        binds={"limit": limit},
    )


def get_top_sql(
    limit: int = 10,
    active_within_minutes: int = 60,
) -> list[dict[str, Any]]:
    """Return recently active SQL ranked by cumulative elapsed time."""
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    if not 1 <= active_within_minutes <= 1440:
        raise ValueError("active_within_minutes must be between 1 and 1440")

    settings = get_settings()

    return query(
        """
        SELECT *
        FROM (
            SELECT
                s.sql_id,
                SUBSTR(s.sql_text, 1, 300) AS sql_text,
                s.executions,
                s.parse_calls,
                s.buffer_gets,
                s.disk_reads,
                s.rows_processed,
                ROUND(s.elapsed_time / 1000000, 3) AS elapsed_seconds,
                ROUND(s.cpu_time / 1000000, 3) AS cpu_seconds,
                ROUND(
                    s.elapsed_time / NULLIF(s.executions, 0) / 1000,
                    3
                ) AS avg_elapsed_ms,
                s.last_active_time,
                c.name AS con_name
            FROM v$sql s
            LEFT JOIN v$containers c
              ON c.con_id = s.con_id
            WHERE s.sql_id IS NOT NULL
              AND s.last_active_time >= SYSDATE - (:active_minutes / 1440)
            ORDER BY s.elapsed_time DESC
        )
        WHERE ROWNUM <= :limit
        """,
        database_name=settings.oracle_cdb_name,
        binds={
            "active_minutes": active_within_minutes,
            "limit": limit,
        },
    )


def get_wait_events(
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return top non-idle system wait events by cumulative wait time."""
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")

    settings = get_settings()

    return query(
        """
        SELECT *
        FROM (
            SELECT
                event,
                wait_class,
                total_waits,
                ROUND(time_waited_micro / 1000000, 3) AS time_waited_seconds,
                ROUND(
                    time_waited_micro / NULLIF(total_waits, 0) / 1000,
                    3
                ) AS avg_wait_ms
            FROM v$system_event
            WHERE wait_class != 'Idle'
            ORDER BY time_waited_micro DESC
        )
        WHERE ROWNUM <= :limit
        """,
        database_name=settings.oracle_cdb_name,
        binds={"limit": limit},
    )


def get_tablespace_usage(
    pdb_name: str | None = None,
) -> list[dict[str, Any]]:
    """Return tablespace usage for all containers or one validated PDB.

    Two percentages, because they answer different questions:

    pct_used          used / currently ALLOCATED size. High on any
                      autoextensible tablespace that has not grown yet,
                      so on its own it is not a capacity signal.
    pct_used_of_max   used / the size the files can actually reach:
                      MAXSIZE for autoextensible files, current size
                      otherwise. This is the one to alert on.

    Neither accounts for free space on the filesystem underneath, so a
    MAXSIZE larger than the disk is not caught here.
    """
    settings = get_settings()

    validated_pdb_name = (
        validate_pdb_name(pdb_name)
        if pdb_name is not None
        else None
    )

    pdb_filter = (
        "WHERE UPPER(c.name) = :pdb_name"
        if validated_pdb_name is not None
        else ""
    )
    binds = (
        {"pdb_name": validated_pdb_name}
        if validated_pdb_name is not None
        else None
    )

    return query(
        f"""
        SELECT c.name AS con_name,
               df.tablespace_name,
               ROUND(df.bytes_gb, 2) AS total_gb,
               ROUND(df.bytes_gb - NVL(fs.free_gb, 0), 2) AS used_gb,
               ROUND(NVL(fs.free_gb, 0), 2) AS free_gb,
               ROUND(
                   (1 - NVL(fs.free_gb, 0) / NULLIF(df.bytes_gb, 0)) * 100,
                   2
               ) AS pct_used,
               ROUND(df.max_gb, 2) AS max_gb,
               ROUND(
                   (df.bytes_gb - NVL(fs.free_gb, 0))
                     / NULLIF(df.max_gb, 0) * 100,
                   2
               ) AS pct_used_of_max,
               df.autoextensible
        FROM (
            SELECT con_id,
                   tablespace_name,
                   SUM(bytes) / 1024 / 1024 / 1024 AS bytes_gb,
                   -- A file can be larger than its MAXSIZE after a manual
                   -- resize, hence GREATEST.
                   SUM(
                       CASE
                           WHEN autoextensible = 'YES'
                           THEN GREATEST(maxbytes, bytes)
                           ELSE bytes
                       END
                   ) / 1024 / 1024 / 1024 AS max_gb,
                   -- 'YES' if any file in the tablespace can grow.
                   MAX(autoextensible) AS autoextensible
            FROM cdb_data_files
            GROUP BY con_id, tablespace_name
        ) df
        LEFT JOIN (
            SELECT con_id,
                   tablespace_name,
                   SUM(bytes) / 1024 / 1024 / 1024 AS free_gb
            FROM cdb_free_space
            GROUP BY con_id, tablespace_name
        ) fs
          ON df.con_id = fs.con_id
         AND df.tablespace_name = fs.tablespace_name
        JOIN v$containers c
          ON c.con_id = df.con_id
        {pdb_filter}
        ORDER BY c.name, df.tablespace_name
        """,
        database_name=settings.oracle_cdb_name,
        binds=binds,
    )


def get_tablespace_diagnostics(
    pdb_name: str,
    tablespace_name: str,
) -> list[dict[str, Any]]:
    """Return reviewed file-level capacity details for one PDB tablespace."""
    settings = get_settings()
    validated_pdb_name = validate_pdb_name(pdb_name)
    normalized_tablespace = tablespace_name.strip().upper()

    if not normalized_tablespace:
        raise ValueError("tablespace_name must not be empty")

    return query(
        """
        SELECT c.name AS con_name,
               df.tablespace_name,
               df.file_id,
               df.file_name,
               df.autoextensible,
               ROUND(df.bytes / 1024 / 1024 / 1024, 3) AS current_gb,
               ROUND(
                   CASE
                       WHEN df.autoextensible = 'YES' THEN df.maxbytes
                       ELSE df.bytes
                   END / 1024 / 1024 / 1024,
                   3
               ) AS effective_max_gb,
               ROUND(df.increment_by * ts.block_size / 1024 / 1024, 2)
                   AS next_extend_mb,
               ROUND(NVL(fs.free_bytes, 0) / 1024 / 1024 / 1024, 3)
                   AS tablespace_free_gb
        FROM cdb_data_files df
        JOIN v$containers c
          ON c.con_id = df.con_id
        JOIN cdb_tablespaces ts
          ON ts.con_id = df.con_id
         AND ts.tablespace_name = df.tablespace_name
        LEFT JOIN (
            SELECT con_id,
                   tablespace_name,
                   SUM(bytes) AS free_bytes
            FROM cdb_free_space
            GROUP BY con_id, tablespace_name
        ) fs
          ON fs.con_id = df.con_id
         AND fs.tablespace_name = df.tablespace_name
        WHERE UPPER(c.name) = :pdb_name
          AND UPPER(df.tablespace_name) = :tablespace_name
        ORDER BY df.file_id
        """,
        database_name=settings.oracle_cdb_name,
        binds={
            "pdb_name": validated_pdb_name,
            "tablespace_name": normalized_tablespace,
        },
    )



def list_users(
    pdb_name: str,
) -> list[dict[str, Any]]:
    """Return database users from one validated pluggable database."""
    validated_pdb_name = validate_pdb_name(pdb_name)

    return query(
        """
        SELECT username,
               account_status,
               default_tablespace,
               temporary_tablespace,
               profile,
               authentication_type,
               common,
               oracle_maintained,
               created
        FROM dba_users
        ORDER BY username
        """,
        database_name=validated_pdb_name,
    )


def get_long_running_sessions(
    threshold_seconds: int,
) -> list[dict[str, Any]]:
    """Return active user sessions running at least threshold_seconds."""
    if threshold_seconds < 1:
        raise ValueError("threshold_seconds must be at least 1")

    settings = get_settings()

    return query(
        """
        SELECT s.sid,
               s.serial# AS serial_num,
               s.username,
               s.sql_id,
               s.last_call_et AS seconds_running,
               s.machine,
               s.module,
               s.event,
               s.wait_class,
               c.name AS con_name
        FROM v$session s
        LEFT JOIN v$containers c
          ON c.con_id = s.con_id
        WHERE s.status = 'ACTIVE'
          AND s.type = 'USER'
          AND s.username IS NOT NULL
          AND s.last_call_et >= :threshold
        ORDER BY s.last_call_et DESC
        """,
        database_name=settings.oracle_cdb_name,
        binds={"threshold": threshold_seconds},
    )


def get_alert_log_errors(
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent ORA- errors from V$DIAG_ALERT_EXT."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")

    settings = get_settings()

    return query(
        """
        SELECT *
        FROM (
            SELECT TO_CHAR(
                       originating_timestamp,
                       'YYYY-MM-DD HH24:MI:SS'
                   ) AS logged_at,
                   message_text
            FROM v$diag_alert_ext
            WHERE originating_timestamp >
                  SYSTIMESTAMP - INTERVAL '1' HOUR
              AND REGEXP_LIKE(message_text, 'ORA-[0-9]+')
            ORDER BY originating_timestamp DESC
        )
        WHERE ROWNUM <= :limit
        """,
        database_name=settings.oracle_cdb_name,
        binds={"limit": limit},
    )


def get_write_workload_counters() -> list[dict[str, Any]]:
    """Return cumulative counters used by the WRITE_WORKLOAD monitor."""
    settings = get_settings()

    return query(
        """
        SELECT name, value
        FROM v$sysstat
        WHERE name IN (
            'user commits',
            'redo size',
            'execute count',
            'user calls'
        )
        """,
        database_name=settings.oracle_cdb_name,
    )

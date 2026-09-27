"""Small Oracle database access layer."""

from typing import Any

import oracledb

from oracle_core.config import get_settings


# Fetch CLOB and BLOB columns as str/bytes instead of LOB locators.
#
# query() closes its connection before returning, so a locator handed
# back to the caller is already dead - reading it fails with
# "DPY-1001: not connected to database". This matters for v$sql.
# sql_fulltext in oracle_core/sql_details.py.
oracledb.defaults.fetch_lobs = False


def build_service_name(database_name: str) -> str:
    """Build an Oracle service name from a database name and domain."""
    settings = get_settings()

    clean_name = database_name.strip()

    if not clean_name:
        raise ValueError("Database name must not be empty")

    domain = (settings.oracle_domain or "").strip().lstrip(".")

    if "." in clean_name:
        return clean_name

    if not domain:
        return clean_name

    return f"{clean_name}.{domain}"


def build_dsn(database_name: str) -> str:
    """Build an Oracle Easy Connect DSN."""
    settings = get_settings()
    service_name = build_service_name(database_name)

    return (
        f"{settings.oracle_host}:"
        f"{settings.oracle_port}/"
        f"{service_name}"
    )


def _rows_from_cursor(cursor: Any) -> list[dict[str, Any]]:
    """Convert a cursor's result set into a list of dictionaries."""
    if cursor.description is None:
        return []

    column_names = [
        column[0].lower()
        for column in cursor.description
    ]

    return [
        dict(zip(column_names, row))
        for row in cursor.fetchall()
    ]


def query(
    sql: str,
    *,
    database_name: str,
    binds: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute an Oracle query and return its rows as dictionaries."""
    settings = get_settings()
    dsn = build_dsn(database_name)

    with oracledb.connect(
        user=settings.oracle_user,
        password=settings.oracle_password.get_secret_value(),
        dsn=dsn,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, binds or {})
            return _rows_from_cursor(cursor)


def execute_statements(
    statements: list[str],
    *,
    database_name: str,
) -> list[dict[str, Any]]:
    """Run statements (typically DDL) in order on one database or PDB.

    Stops at the first failure: later statements usually depend on
    earlier ones, and a partial run is easier to reason about than one
    that pressed on. Each statement is reported as OK, ERROR or SKIPPED.
    DDL commits implicitly, so there is nothing to roll back.
    """
    settings = get_settings()
    dsn = build_dsn(database_name)
    results: list[dict[str, Any]] = []

    with oracledb.connect(
        user=settings.oracle_user,
        password=settings.oracle_password.get_secret_value(),
        dsn=dsn,
    ) as connection:
        with connection.cursor() as cursor:
            failed = False
            for statement in statements:
                if failed:
                    results.append({"statement": statement, "status": "SKIPPED"})
                    continue
                try:
                    cursor.execute(statement)
                    results.append({"statement": statement, "status": "OK"})
                except Exception as exc:  # noqa: BLE001 - report per statement
                    failed = True
                    results.append(
                        {
                            "statement": statement,
                            "status": "ERROR",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )

    return results


def query_dsn(
    sql: str,
    *,
    dsn: str,
    user: str,
    password: str,
    binds: dict[str, Any] | None = None,
    sysdba: bool = False,
) -> list[dict[str, Any]]:
    """Execute a query against an explicit DSN with explicit credentials.

    Needed for databases that build_dsn() cannot reach, such as a Data
    Guard standby on a different host or port.

    A MOUNTED standby is not open, so ordinary sessions are refused with
    ORA-01033. Pass sysdba=True to connect as SYSDBA, which requires the
    remote password file and matching SYS credentials.
    """
    clean_dsn = (dsn or "").strip()

    if not clean_dsn:
        raise ValueError("DSN must not be empty")

    connect_kwargs: dict[str, Any] = {
        "user": user,
        "password": password,
        "dsn": clean_dsn,
        # Fail fast rather than hanging a chat turn on an unreachable host.
        "tcp_connect_timeout": 10,
    }

    if sysdba:
        connect_kwargs["mode"] = oracledb.AUTH_MODE_SYSDBA

    with oracledb.connect(**connect_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, binds or {})
            return _rows_from_cursor(cursor)

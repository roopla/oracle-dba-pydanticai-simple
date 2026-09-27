"""Monthly range-partitioned tables and their retention.

Read-only. Finds tables range-partitioned by month on a single DATE or
TIMESTAMP column, reads each partition's month from its HIGH_VALUE, and
works out which partitions fall outside a retention window. The drop
itself is an allowlisted remediation action in oracle_core.remediation.

Month arithmetic uses the DATABASE's current month (TRUNC(SYSDATE,'MM')),
not this machine's clock, so "current month" is the month the data is
being written in.

Retention rule: keep the newest `keep_months` months (at least 2: the
current and the previous month). A partition is a drop candidate only when
BOTH hold:
  - its upper bound is at or before the first day of the oldest kept month
    (all of its rows are older than the window), and
  - it is not among the newest `keep_months` partitions by position -
    a second guard in case the newest months have no partitions yet.
MAXVALUE partitions are never candidates.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from oracle_core.db import query

MIN_KEEP_MONTHS = 2
MAX_KEEP_MONTHS = 120

_MB = 1024 * 1024

# HIGH_VALUE is stored as SQL text, e.g.
#   TO_DATE(' 2026-09-01 00:00:00', 'SYYYY-MM-DD HH24:MI:SS', 'NLS_CALENDAR=GREGORIAN')
#   TIMESTAMP' 2026-09-01 00:00:00'
_HIGH_VALUE_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})")


def parse_high_value(text: str | None) -> date | None:
    """Return the upper bound date of a partition, or None for MAXVALUE.

    Raises ValueError for anything that is not a plain date bound, so an
    unexpected format can never be mistaken for an old partition.
    """
    value = (text or "").strip()

    if value.upper() == "MAXVALUE":
        return None

    match = _HIGH_VALUE_DATE.search(value)
    if not match:
        raise ValueError(f"Unrecognised partition bound: {value[:80]}")

    year, month, day, hour, minute, second = (int(g) for g in match.groups())
    if (hour, minute, second) != (0, 0, 0):
        raise ValueError(f"Partition bound is not midnight: {value[:80]}")

    return date(year, month, day)


def add_months(start: date, months: int) -> date:
    """First day of the month `months` away from `start`'s month."""
    index = start.year * 12 + (start.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def data_month(high_value: date) -> str:
    """The month a monthly partition holds: the one before its bound."""
    month = add_months(high_value, -1)
    return f"{month.year:04d}-{month.month:02d}"


def retention_cutoff(current_month: date, keep_months: int) -> date:
    """Partitions whose bound is at or before this hold only older data."""
    return add_months(current_month, -(keep_months - 1))


def validate_keep_months(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("keep_months must be a whole number")
    try:
        keep = int(value)
    except ValueError as exc:
        raise ValueError("keep_months must be a whole number") from exc
    if not MIN_KEEP_MONTHS <= keep <= MAX_KEEP_MONTHS:
        raise ValueError(
            f"keep_months must be between {MIN_KEEP_MONTHS} and "
            f"{MAX_KEEP_MONTHS}: at least the current and the previous "
            "month are always kept"
        )
    return keep


def _is_monthly_interval(interval: str | None) -> bool:
    compact = re.sub(r"\s+", "", (interval or "").upper())
    return compact in {"NUMTOYMINTERVAL(1,'MONTH')", "INTERVAL'1'MONTH"}


def database_current_month(pdb_name: str) -> date:
    row = query(
        "SELECT TRUNC(SYSDATE, 'MM') AS m FROM dual",
        database_name=pdb_name,
    )[0]
    value = row["m"]
    return date(value.year, value.month, 1)


def monthly_partitioned_tables(
    pdb_name: str,
    tablespace_name: str | None = None,
) -> list[dict[str, Any]]:
    """Single-column DATE/TIMESTAMP range-partitioned tables in a PDB.

    Tables owned by Oracle-maintained schemas and composite-partitioned
    tables are left out. When tablespace_name is given, only tables with at
    least one partition in that tablespace are returned. Whether the
    boundaries are really monthly is checked per table by
    table_partitions().
    """
    tablespace_filter = (
        """
          AND EXISTS (
                SELECT 1 FROM dba_tab_partitions tp
                WHERE tp.table_owner = t.owner
                  AND tp.table_name = t.table_name
                  AND tp.tablespace_name = :tablespace)
        """
        if tablespace_name
        else ""
    )
    binds = {"tablespace": tablespace_name.upper()} if tablespace_name else None

    return query(
        f"""
        SELECT t.owner,
               t.table_name,
               t.interval,
               k.column_name AS partition_key,
               col.data_type AS key_type
        FROM dba_part_tables t
        JOIN dba_part_key_columns k
          ON k.owner = t.owner
         AND k.name = t.table_name
         AND k.object_type = 'TABLE'
        JOIN dba_tab_columns col
          ON col.owner = t.owner
         AND col.table_name = t.table_name
         AND col.column_name = k.column_name
        JOIN dba_users u
          ON u.username = t.owner
        WHERE t.partitioning_type = 'RANGE'
          AND t.subpartitioning_type = 'NONE'
          AND t.partitioning_key_count = 1
          AND u.oracle_maintained = 'N'
          AND (col.data_type = 'DATE' OR col.data_type LIKE 'TIMESTAMP%')
          {tablespace_filter}
        ORDER BY t.owner, t.table_name
        """,
        database_name=pdb_name,
        binds=binds,
    )


def table_partitions(pdb_name: str, owner: str, table_name: str) -> dict[str, Any]:
    """One table's partitions with month, size and bound, oldest first.

    Raises ValueError when the table is not range-partitioned by month.
    """
    tables = [
        t
        for t in monthly_partitioned_tables(pdb_name)
        if t["owner"] == owner and t["table_name"] == table_name
    ]
    if not tables:
        raise ValueError(
            f"{owner}.{table_name} in {pdb_name} is not a single-column "
            "DATE/TIMESTAMP range-partitioned table owned by an application "
            "schema"
        )
    table = tables[0]

    if table["interval"] and not _is_monthly_interval(table["interval"]):
        raise ValueError(
            f"{owner}.{table_name} uses interval {table['interval']}, not one month"
        )

    rows = query(
        """
        SELECT p.partition_name,
               p.partition_position,
               p.high_value,
               p.tablespace_name,
               p.interval,
               NVL(s.bytes, 0) AS bytes,
               p.num_rows
        FROM dba_tab_partitions p
        LEFT JOIN (
            SELECT partition_name, SUM(bytes) AS bytes
            FROM dba_segments
            WHERE owner = :owner
              AND segment_name = :table_name
              AND segment_type = 'TABLE PARTITION'
            GROUP BY partition_name
        ) s
          ON s.partition_name = p.partition_name
        WHERE p.table_owner = :owner
          AND p.table_name = :table_name
        ORDER BY p.partition_position
        """,
        database_name=pdb_name,
        binds={"owner": owner, "table_name": table_name},
    )

    partitions = []
    for row in rows:
        bound = parse_high_value(row["high_value"])
        if bound is not None and bound.day != 1:
            raise ValueError(
                f"{owner}.{table_name} is not partitioned by month: partition "
                f"{row['partition_name']} ends on {bound.isoformat()}"
            )
        partitions.append(
            {
                "partition_name": row["partition_name"],
                "position": int(row["partition_position"]),
                "high_value": bound.isoformat() if bound else "MAXVALUE",
                "month": data_month(bound) if bound else "MAXVALUE",
                "tablespace_name": row["tablespace_name"],
                "size_mb": round(int(row["bytes"] or 0) / _MB, 1),
                "interval_section": row["interval"] == "YES",
            }
        )

    return {
        "owner": owner,
        "table_name": table_name,
        "partition_key": table["partition_key"],
        "interval": table["interval"],
        "partitions": partitions,
    }


def retention_plan(
    pdb_name: str,
    owner: str,
    table_name: str,
    keep_months: int = MIN_KEEP_MONTHS,
    current_month: date | None = None,
) -> dict[str, Any]:
    """Which partitions a retention of keep_months would drop, and why."""
    keep = validate_keep_months(keep_months)
    info = table_partitions(pdb_name, owner, table_name)
    month = current_month or database_current_month(pdb_name)
    cutoff = retention_cutoff(month, keep)

    partitions = info["partitions"]
    dated = [p for p in partitions if p["high_value"] != "MAXVALUE"]
    newest = {p["partition_name"] for p in dated[-keep:]}

    drop = [
        p
        for p in dated
        if date.fromisoformat(p["high_value"]) <= cutoff
        and p["partition_name"] not in newest
    ]
    keep_list = [p for p in partitions if p not in drop]

    return {
        **info,
        "pdb_name": pdb_name,
        "current_month": f"{month.year:04d}-{month.month:02d}",
        "keep_months": keep,
        "oldest_kept_month": f"{cutoff.year:04d}-{cutoff.month:02d}",
        "drop": drop,
        "keep": keep_list,
        "drop_size_mb": round(sum(p["size_mb"] for p in drop), 1),
    }

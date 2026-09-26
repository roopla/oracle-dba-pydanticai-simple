"""Full SQL text and plan lookup.

Save as oracle_core/sql_details.py.

get_top_sql() truncates sql_text to 300 characters so that large result
sets do not flood the agent's conversation history. This module fetches
the untruncated text for a single sql_id on demand, which is what the
"Show full SQL" action button in the chat UI calls.
"""

from __future__ import annotations

import re
from typing import Any

from oracle_core.config import get_settings
from oracle_core.db import query


# sql_id is always 13 characters of lowercase alphanumerics.
_SQL_ID_PATTERN = re.compile(r"^[0-9a-z]{13}$")

MAX_SQL_TEXT_CHARS = 20000


def validate_sql_id(sql_id: str) -> str:
    """Return a validated sql_id or raise.

    sql_id is interpolated nowhere - it is always bound - but validating
    keeps malformed input out of the round trip entirely.
    """
    clean = (sql_id or "").strip().lower()

    if not _SQL_ID_PATTERN.match(clean):
        raise ValueError(
            f"Invalid sql_id: {sql_id!r}. Expected 13 alphanumeric characters."
        )

    return clean


def get_full_sql_text(
    sql_id: str,
    pdb_name: str | None = None,
) -> dict[str, Any]:
    """Return the complete SQL text and key statistics for one sql_id.

    Reads the shared pool, so the cursor must still be cached. A cursor
    that has aged out returns found=False rather than raising.
    """
    validated_sql_id = validate_sql_id(sql_id)

    settings = get_settings()
    database_name = pdb_name or settings.oracle_cdb_name

    rows = query(
        """
        SELECT
            s.sql_id,
            s.sql_fulltext,
            s.executions,
            s.plan_hash_value,
            s.parsing_schema_name,
            ROUND(s.elapsed_time / 1000000, 3)                AS elapsed_seconds,
            ROUND(s.cpu_time / 1000000, 3)                    AS cpu_seconds,
            s.buffer_gets,
            s.disk_reads,
            s.rows_processed,
            ROUND(s.elapsed_time / NULLIF(s.executions, 0) / 1000, 3)
                                                              AS avg_elapsed_ms,
            s.last_active_time,
            c.name                                            AS con_name
        FROM v$sql s
        LEFT JOIN v$containers c
          ON c.con_id = s.con_id
        WHERE s.sql_id = :sql_id
        ORDER BY s.last_active_time DESC
        FETCH FIRST 1 ROWS ONLY
        """,
        database_name=database_name,
        binds={"sql_id": validated_sql_id},
    )

    if not rows:
        return {
            "found": False,
            "sql_id": validated_sql_id,
            "message": (
                "No cursor found for this sql_id in the shared pool. "
                "It may have aged out."
            ),
        }

    row = dict(rows[0])

    # sql_fulltext is a CLOB; oracledb returns it as str, but guard anyway.
    full_text = row.get("sql_fulltext")

    if full_text is not None and not isinstance(full_text, str):
        full_text = full_text.read()

    if full_text and len(full_text) > MAX_SQL_TEXT_CHARS:
        full_text = full_text[:MAX_SQL_TEXT_CHARS] + "\n... [truncated]"

    row["sql_fulltext"] = full_text
    row["found"] = True

    return row

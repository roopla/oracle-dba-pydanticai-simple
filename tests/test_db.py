"""Block 1 test: connect directly to the default Oracle PDB."""

from oracle_core.config import get_settings
from oracle_core.db import (
    build_dsn,
    build_service_name,
    query,
)


settings = get_settings()
database_name = settings.oracle_default_pdb_name

print("Database name:", database_name)
print("Service name:", build_service_name(database_name))
print("Connecting to:", build_dsn(database_name))

rows = query(
    """
    SELECT
        SYS_CONTEXT('USERENV', 'CON_NAME') AS container_name,
        SYS_CONTEXT('USERENV', 'SERVICE_NAME') AS service_name
    FROM dual
    """,
    database_name=database_name,
)

print("Result:")
print(rows)
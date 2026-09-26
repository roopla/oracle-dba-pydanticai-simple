"""Block 11 test: retrieve core Oracle database status information."""

from oracle_core.config import get_settings
from oracle_core.queries import (
    get_database_identity,
    get_database_role,
    get_database_startup_time,
    get_instance_status,
    get_pdb_status,
)


settings = get_settings()


print("Database identity:")
identity_rows = get_database_identity()

for row in identity_rows:
    print(row)

assert len(identity_rows) == 1, (
    "Database identity query must return exactly one row"
)

identity = identity_rows[0]

for required_key in (
    "name",
    "db_unique_name",
    "dbid",
    "cdb",
):
    assert required_key in identity, (
        f"Database identity is missing {required_key!r}"
    )


print("\nInstance status:")
instance_rows = get_instance_status()

for row in instance_rows:
    print(row)

assert len(instance_rows) == 1, (
    "Instance status query must return exactly one row"
)

instance = instance_rows[0]

for required_key in (
    "instance_name",
    "host_name",
    "version",
    "status",
    "database_status",
    "instance_role",
):
    assert required_key in instance, (
        f"Instance status is missing {required_key!r}"
    )

assert instance["status"], "Instance status must not be empty"
assert instance["database_status"], (
    "Database status must not be empty"
)


print("\nDatabase role:")
role_rows = get_database_role()

for row in role_rows:
    print(row)

assert len(role_rows) == 1, (
    "Database role query must return exactly one row"
)

role = role_rows[0]

for required_key in (
    "database_role",
    "open_mode",
    "protection_mode",
    "protection_level",
    "switchover_status",
):
    assert required_key in role, (
        f"Database role is missing {required_key!r}"
    )

assert role["database_role"], (
    "Database role must not be empty"
)


print("\nDatabase startup time:")
startup_rows = get_database_startup_time()

for row in startup_rows:
    print(row)

assert len(startup_rows) == 1, (
    "Database startup-time query must return exactly one row"
)
assert startup_rows[0]["startup_time"] is not None, (
    "Database startup time must not be empty"
)


print("\nDefault PDB status:")
pdb_rows = get_pdb_status()

for row in pdb_rows:
    print(row)

assert len(pdb_rows) == 1, (
    "PDB status query must return exactly one row"
)
assert pdb_rows[0]["name"] == (
    settings.oracle_default_pdb_name.strip().upper()
), "The returned PDB does not match the configured default PDB"
assert pdb_rows[0]["open_mode"], (
    "PDB open mode must not be empty"
)


print("\nTesting an invalid PDB name:")

try:
    get_pdb_status("BADPDB")
except ValueError as exc:
    print("Rejected BADPDB:", exc)
    assert "Unknown PDB 'BADPDB'" in str(exc)
else:
    raise AssertionError("BADPDB was unexpectedly accepted")


print("\nBlock 11 passed")

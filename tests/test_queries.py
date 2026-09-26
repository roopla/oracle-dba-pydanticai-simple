"""Block 2 test: run plain Python Oracle DBA functions."""

from oracle_core.queries import (
    get_database_version,
    list_pdbs,
)


print("Database version using the default PDB:")
version_rows = get_database_version()

for row in version_rows:
    print(row)


print("\nDatabase version using ORCLPDB2:")
version_rows_pdb2 = get_database_version("ORCLPDB2")

for row in version_rows_pdb2:
    print(row)


print("\nPluggable databases:")
pdb_rows = list_pdbs()

for row in pdb_rows:
    print(row)


assert version_rows, "Database version query returned no rows"
assert version_rows_pdb2, "ORCLPDB2 version query returned no rows"

assert any(
    row["name"] == "ORCLPDB1"
    for row in pdb_rows
), "ORCLPDB1 was not returned"

assert any(
    row["name"] == "ORCLPDB2"
    for row in pdb_rows
), "ORCLPDB2 was not returned"

print("\nBlock 2 passed")

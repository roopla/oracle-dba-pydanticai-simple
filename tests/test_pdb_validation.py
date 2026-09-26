"""Block 8 test: validate Oracle PDB selection."""

from oracle_core.queries import (
    get_database_version,
    normalize_pdb_name,
    validate_pdb_name,
)


def expect_validation_error(
    pdb_name: str,
    expected_message: str,
) -> None:
    """Confirm that a PDB value produces the expected error."""
    try:
        validate_pdb_name(pdb_name)
    except ValueError as exc:
        message = str(exc)

        print(f"Rejected {pdb_name!r}: {message}")

        assert expected_message in message, (
            f"Expected {expected_message!r} in {message!r}"
        )
    else:
        raise AssertionError(
            f"Expected {pdb_name!r} to be rejected"
        )


print("Testing normalization:")

normalized_name = normalize_pdb_name(
    "  orclpdb2  "
)

print("Normalized value:", normalized_name)

assert normalized_name == "ORCLPDB2"


print("\nTesting valid PDB names:")

validated_pdb1 = validate_pdb_name("ORCLPDB1")
validated_pdb2 = validate_pdb_name("orclpdb2")

print("Validated:", validated_pdb1)
print("Validated:", validated_pdb2)

assert validated_pdb1 == "ORCLPDB1"
assert validated_pdb2 == "ORCLPDB2"


print("\nTesting a real query using lowercase input:")

version_rows = get_database_version("orclpdb2")

for row in version_rows:
    print(row)

assert version_rows, (
    "The validated ORCLPDB2 query returned no rows"
)


print("\nTesting invalid PDB names:")

expect_validation_error(
    "",
    "PDB name must not be empty",
)

expect_validation_error(
    "   ",
    "PDB name must not be empty",
)

expect_validation_error(
    "BADPDB",
    "Unknown PDB 'BADPDB'",
)

expect_validation_error(
    "PDB$SEED",
    "protected template PDB",
)


print("\nBlock 8 passed")
"""Validates a downloaded or post-run SQLite state file without mutating it."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from djinni_scraper.persistence.sqlite import (
    DISCOVERIES_TABLE,
    OUTBOX_TABLE,
    PUBLICATIONS_TABLE,
    SCHEMA_VERSION,
    VACANCIES_TABLE,
    VERSIONS_TABLE,
    validate_supported_schema,
)


CURRENT_TABLES = frozenset(
    {VACANCIES_TABLE, PUBLICATIONS_TABLE, DISCOVERIES_TABLE, OUTBOX_TABLE, VERSIONS_TABLE}
)


def validate_database(path: Path, *, require_current_schema: bool) -> tuple[bool, str]:
    """Checks integrity plus the schema contract, opening the file read-only."""
    if not path.is_file():
        return False, f"SQLite state file is missing: {path}"

    try:
        database_uri = f"{path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(database_uri, uri=True) as connection:
            quick_check = connection.execute("PRAGMA quick_check;").fetchone()
            foreign_key_violations = list(connection.execute("PRAGMA foreign_key_check;"))
            schema_version = validate_supported_schema(connection)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table';"
                )
            }
    except RuntimeError as error:
        return False, str(error)
    except (OSError, sqlite3.Error) as error:
        return False, f"Unable to read SQLite state: {type(error).__name__}"

    if quick_check != ("ok",):
        return False, "SQLite quick_check did not return ok"
    if foreign_key_violations:
        return False, "SQLite foreign_key_check reported violations"
    if require_current_schema:
        if schema_version != SCHEMA_VERSION:
            return False, f"Expected schema version {SCHEMA_VERSION}, got {schema_version}"
        missing_tables = CURRENT_TABLES - tables
        if missing_tables:
            return False, f"SQLite state is missing required tables: {', '.join(sorted(missing_tables))}"

    return True, f"SQLite state is valid (schema version {schema_version})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--allow-supported-schema",
        action="store_true",
        help="allow repository-supported pre-migration schemas",
    )
    mode.add_argument(
        "--require-current-schema",
        action="store_true",
        help="require the repository's current schema and all current tables",
    )
    parser.add_argument("database_path", type=Path)
    args = parser.parse_args(argv)

    valid, message = validate_database(
        args.database_path,
        require_current_schema=args.require_current_schema,
    )
    output = sys.stdout if valid else sys.stderr
    print(message, file=output)
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())

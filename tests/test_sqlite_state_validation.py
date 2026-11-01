import sqlite3
import subprocess
import sys
from pathlib import Path
from runpy import run_path

from djinni_scraper.persistence import sqlite as sqlite_module
from djinni_scraper.persistence.sqlite import SQLiteVacancyRepository


ROOT = Path(__file__).parents[1]
SCRIPT = run_path(str(ROOT / "scripts" / "validate_sqlite_state.py"))
validate_database = SCRIPT["validate_database"]


def test_current_database_passes_pre_and_post_run_validation(tmp_path):
    path = tmp_path / "vacancies.db"
    repository = SQLiteVacancyRepository(path)
    repository.open()
    repository.close()

    assert validate_database(path, require_current_schema=False)[0] is True
    assert validate_database(path, require_current_schema=True)[0] is True
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "validate_sqlite_state.py"),
            "--require-current-schema",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "schema version 4" in result.stdout


def test_supported_older_database_is_allowed_only_before_application_migration(tmp_path):
    path = tmp_path / "v1.db"
    connection = sqlite3.connect(path)
    try:
        legacy_columns = ", ".join(
            f"{column} TEXT"
            for column in sorted(sqlite_module._V1_CANONICAL_COLUMNS - {"id"})
        )
        connection.execute(f"CREATE TABLE vacancies (id INTEGER PRIMARY KEY, {legacy_columns})")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()

    assert validate_database(path, require_current_schema=False)[0] is True
    assert validate_database(path, require_current_schema=True)[0] is False


def test_unknown_schema_is_rejected_before_crawler_can_bootstrap_over_it(tmp_path):
    path = tmp_path / "future.db"
    repository = SQLiteVacancyRepository(path)
    repository.open()
    repository.close()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 5")
    connection.commit()
    connection.close()

    valid, message = validate_database(path, require_current_schema=False)

    assert valid is False
    assert "Unsupported SQLite schema version" in message

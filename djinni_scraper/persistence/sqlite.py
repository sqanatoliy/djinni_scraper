"""SQLite persistence for canonical Vacancy value objects."""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from djinni_scraper.domain.vacancy import Company, Compensation, LanguageRequirement, Location, Vacancy


SCHEMA_VERSION = 1
VACANCIES_TABLE = "vacancies"
_CANONICAL_COLUMNS = frozenset(
    {
        "id",
        "identity_key",
        "source",
        "source_vacancy_id",
        "url",
        "title",
        "description",
        "company_name",
        "company_url",
        "salary_min",
        "salary_max",
        "salary_currency",
        "salary_unit",
        "employment_type",
        "experience_months",
        "locations_json",
        "applicant_locations_json",
        "category",
        "industry",
        "tags_json",
        "language_requirements_json",
        "published_at",
        "valid_through",
        "scraped_at",
        "discovery_profiles_json",
        "raw_source_metadata_json",
    }
)


@dataclass(frozen=True, slots=True)
class InsertResult:
    """The persistence outcome used by the temporary Telegram bridge."""

    inserted: bool
    vacancy_id: int


class SQLiteVacancyRepository:
    """Small canonical SQLite boundary with idempotent schema initialization."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.connection: sqlite3.Connection | None = None

    def open(self) -> None:
        """Opens one connection and initializes the canonical schema safely."""
        if self.connection is not None:
            return
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        try:
            initialize_schema(self.connection)
        except Exception:
            self.connection.close()
            self.connection = None
            raise

    def close(self) -> None:
        """Closes the owned SQLite connection."""
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def insert_vacancy(self, vacancy: Vacancy) -> InsertResult:
        """Commits a canonical vacancy and reports whether it was newly persisted."""
        connection = self._require_connection()
        record = vacancy_to_record(vacancy)
        try:
            cursor = connection.execute(
                """
                INSERT INTO vacancies (
                    identity_key, source, source_vacancy_id, url, title, description,
                    company_name, company_url,
                    salary_min, salary_max, salary_currency, salary_unit,
                    employment_type, experience_months,
                    locations_json, applicant_locations_json,
                    category, industry, tags_json, language_requirements_json,
                    published_at, valid_through, scraped_at,
                    discovery_profiles_json, raw_source_metadata_json
                ) VALUES (
                    :identity_key, :source, :source_vacancy_id, :url, :title, :description,
                    :company_name, :company_url,
                    :salary_min, :salary_max, :salary_currency, :salary_unit,
                    :employment_type, :experience_months,
                    :locations_json, :applicant_locations_json,
                    :category, :industry, :tags_json, :language_requirements_json,
                    :published_at, :valid_through, :scraped_at,
                    :discovery_profiles_json, :raw_source_metadata_json
                ) ON CONFLICT(identity_key) DO NOTHING
                """,
                record,
            )
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            raise

        if cursor.rowcount == 1:
            return InsertResult(inserted=True, vacancy_id=cursor.lastrowid)

        row = connection.execute(
            "SELECT id FROM vacancies WHERE identity_key = ?", (record["identity_key"],)
        ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError("Canonical vacancy insert did not return an inserted or existing row")
        return InsertResult(inserted=False, vacancy_id=row["id"])

    def get_vacancy(self, vacancy: Vacancy) -> Vacancy | None:
        """Reads a vacancy back using the same temporary identity contract."""
        return self.get_vacancy_by_identity_key(vacancy_identity_key(vacancy))

    def get_vacancy_by_identity_key(self, identity_key: str) -> Vacancy | None:
        """Reads one canonical vacancy from its internal identity key."""
        row = self._require_connection().execute(
            "SELECT * FROM vacancies WHERE identity_key = ?", (identity_key,)
        ).fetchone()
        return row_to_vacancy(row) if row else None

    def _require_connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("SQLiteVacancyRepository is not open")
        return self.connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Creates canonical schema v1 or rejects an incompatible existing database."""
    existing_columns = _table_columns(connection, VACANCIES_TABLE)
    if existing_columns and not _CANONICAL_COLUMNS.issubset(existing_columns):
        raise RuntimeError(
            "Existing vacancies table is not canonical schema v1; reset the legacy database before use"
        )

    current_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if current_version not in (0, SCHEMA_VERSION):
        raise RuntimeError(
            f"Unsupported SQLite schema version {current_version}; expected {SCHEMA_VERSION}"
        )

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS vacancies (
            id INTEGER PRIMARY KEY,
            identity_key TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            source_vacancy_id TEXT,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            company_name TEXT,
            company_url TEXT,
            salary_min NUMERIC,
            salary_max NUMERIC,
            salary_currency TEXT,
            salary_unit TEXT,
            employment_type TEXT,
            experience_months NUMERIC,
            locations_json TEXT NOT NULL,
            applicant_locations_json TEXT NOT NULL,
            category TEXT,
            industry TEXT,
            tags_json TEXT NOT NULL,
            language_requirements_json TEXT NOT NULL,
            published_at TEXT,
            valid_through TEXT,
            scraped_at TEXT NOT NULL,
            discovery_profiles_json TEXT NOT NULL,
            raw_source_metadata_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_vacancies_source ON vacancies (source);
        CREATE INDEX IF NOT EXISTS idx_vacancies_published_at ON vacancies (published_at);
        """
    )
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()


def vacancy_identity_key(vacancy: Vacancy) -> str:
    """Temporary duplicate key: source+ID, or source+canonical URL when ID is unavailable."""
    if vacancy.source_vacancy_id is not None:
        return _json_dump([vacancy.source, "source_vacancy_id", vacancy.source_vacancy_id])
    return _json_dump([vacancy.source, "url", vacancy.url])


def vacancy_to_record(vacancy: Vacancy) -> dict[str, Any]:
    """Flattens scalar canonical values and deterministically serializes structures."""
    data = vacancy.to_dict()
    company = data["company"] or {}
    compensation = data["compensation"] or {}
    return {
        "identity_key": vacancy_identity_key(vacancy),
        "source": data["source"],
        "source_vacancy_id": data["source_vacancy_id"],
        "url": data["url"],
        "title": data["title"],
        "description": data["description"],
        "company_name": company.get("name"),
        "company_url": company.get("url"),
        "salary_min": compensation.get("min_amount"),
        "salary_max": compensation.get("max_amount"),
        "salary_currency": compensation.get("currency"),
        "salary_unit": compensation.get("unit"),
        "employment_type": data["employment_type"],
        "experience_months": data["experience_months"],
        "locations_json": _json_dump(data["locations"]),
        "applicant_locations_json": _json_dump(data["applicant_locations"]),
        "category": data["category"],
        "industry": data["industry"],
        "tags_json": _json_dump(data["tags"]),
        "language_requirements_json": _json_dump(data["language_requirements"]),
        "published_at": data["published_at"],
        "valid_through": data["valid_through"],
        "scraped_at": data["scraped_at"],
        "discovery_profiles_json": _json_dump(data["discovery_profiles"]),
        "raw_source_metadata_json": _json_dump(data["raw_source_metadata"]),
    }


def row_to_vacancy(row: Mapping[str, Any]) -> Vacancy:
    """Reconstructs a canonical Vacancy from one SQLite row."""
    company = _company_from_row(row)
    compensation = _compensation_from_row(row)
    return Vacancy(
        source=row["source"],
        source_vacancy_id=row["source_vacancy_id"],
        url=row["url"],
        title=row["title"],
        description=row["description"],
        company=company,
        compensation=compensation,
        employment_type=row["employment_type"],
        experience_months=row["experience_months"],
        locations=tuple(Location(**value) for value in _json_load(row["locations_json"])),
        applicant_locations=tuple(Location(**value) for value in _json_load(row["applicant_locations_json"])),
        category=row["category"],
        industry=row["industry"],
        tags=tuple(_json_load(row["tags_json"])),
        language_requirements=tuple(
            LanguageRequirement(**value) for value in _json_load(row["language_requirements_json"])
        ),
        published_at=_parse_date_or_datetime(row["published_at"]),
        valid_through=_parse_date_or_datetime(row["valid_through"]),
        scraped_at=_parse_datetime(row["scraped_at"]),
        discovery_profiles=tuple(_json_load(row["discovery_profiles_json"])),
        raw_source_metadata=_json_load(row["raw_source_metadata_json"]),
    )


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}


def _company_from_row(row: Mapping[str, Any]) -> Company | None:
    if row["company_name"] is None and row["company_url"] is None:
        return None
    return Company(name=row["company_name"], url=row["company_url"])


def _compensation_from_row(row: Mapping[str, Any]) -> Compensation | None:
    values = (row["salary_min"], row["salary_max"], row["salary_currency"], row["salary_unit"])
    if all(value is None for value in values):
        return None
    return Compensation(
        min_amount=row["salary_min"],
        max_amount=row["salary_max"],
        currency=row["salary_currency"],
        unit=row["salary_unit"],
    )


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: str) -> Any:
    return json.loads(value)


def _parse_date_or_datetime(value: str | None) -> date | datetime | None:
    if value is None:
        return None
    if "T" in value or " " in value:
        return _parse_datetime(value)
    return date.fromisoformat(value)


def _parse_datetime(value: str) -> datetime:
    normalized_value = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(normalized_value)

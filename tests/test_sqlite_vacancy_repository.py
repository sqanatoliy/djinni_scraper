import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from djinni_scraper.domain.vacancy import Company, Compensation, LanguageRequirement, Location, Vacancy
from djinni_scraper.persistence.sqlite import (
    SCHEMA_VERSION,
    SQLiteVacancyRepository,
    vacancy_identity_key,
    vacancy_to_record,
)


SCRAPED_AT = datetime(2026, 8, 13, 9, 30, tzinfo=timezone.utc)


def minimal_vacancy(**overrides) -> Vacancy:
    values = {
        "source": "djinni",
        "source_vacancy_id": "842583",
        "url": "https://djinni.co/jobs/842583-role",
        "title": "Role",
        "scraped_at": SCRAPED_AT,
    }
    values.update(overrides)
    return Vacancy(**values)


def full_vacancy(**overrides) -> Vacancy:
    values = {
        "source": "djinni",
        "source_vacancy_id": "842583",
        "url": "https://djinni.co/jobs/842583-role",
        "title": "Strong Junior AI Engineer",
        "description": "<p>Unicode: Львів</p>",
        "company": Company(name="Softcery", url="https://softcery.com/"),
        "compensation": Compensation(min_amount=1200, max_amount=2000.5, currency="USD", unit="MONTH"),
        "employment_type": "FULL_TIME",
        "experience_months": 12,
        "locations": (Location(country="Україна", city="Львів"), Location(country="Poland", city="Warsaw")),
        "applicant_locations": (Location(region="Europe"),),
        "category": "AI/ML",
        "industry": "other",
        "tags": ("Product company", "AI"),
        "language_requirements": (LanguageRequirement(language="Англійська", level="B2"),),
        "published_at": date(2026, 8, 12),
        "valid_through": datetime(2026, 9, 11, 14, 1, 42),
        "scraped_at": SCRAPED_AT,
        "discovery_profiles": ("data_engineer",),
        "raw_source_metadata": {"site": {"emoji": "✓"}, "jsonld": {"identifier": 842583}},
    }
    values.update(overrides)
    return Vacancy(**values)


@pytest.fixture
def repository(tmp_path):
    repo = SQLiteVacancyRepository(tmp_path / "nested" / "vacancies.db")
    repo.open()
    yield repo
    repo.close()


def test_initialization_creates_schema_v1_without_legacy_columns_and_is_idempotent(repository):
    connection = repository.connection
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    columns = {row[1] for row in connection.execute("PRAGMA table_info(vacancies)")}

    assert "vacancies" in tables
    assert "jobs" not in tables
    assert {"source", "identity_key", "locations_json", "raw_source_metadata_json"}.issubset(columns)
    assert {"truncate_description", "pub_date", "views", "responses", "raw_jsonld"}.isdisjoint(columns)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    indexes = {row[1] for row in connection.execute("PRAGMA index_list(vacancies)")}
    assert {"idx_vacancies_source", "idx_vacancies_published_at"}.issubset(indexes)

    repository.close()
    repository.open()
    assert repository.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_repository_creates_missing_parent_directories_and_supports_custom_paths(tmp_path):
    db_path = tmp_path / "custom" / "state" / "vacancies.db"
    repo = SQLiteVacancyRepository(db_path)

    repo.open()
    assert db_path.exists()
    assert repo.insert_vacancy(minimal_vacancy()).inserted is True
    repo.close()


def test_minimal_vacancy_round_trips_through_sqlite(repository):
    vacancy = minimal_vacancy()
    result = repository.insert_vacancy(vacancy)

    assert result.inserted is True
    assert repository.get_vacancy(vacancy) == vacancy


def test_full_vacancy_round_trips_nested_values_metadata_numbers_and_dates(repository):
    vacancy = full_vacancy()
    repository.insert_vacancy(vacancy)

    restored = repository.get_vacancy(vacancy)
    assert restored == vacancy
    assert isinstance(restored.compensation.max_amount, float)
    assert restored.compensation.max_amount == 2000.5
    assert restored.published_at == date(2026, 8, 12)
    assert restored.valid_through == datetime(2026, 9, 11, 14, 1, 42)
    assert restored.scraped_at.tzinfo is not None
    assert restored.raw_source_metadata["site"]["emoji"] == "✓"


def test_aware_and_naive_source_datetimes_preserve_their_original_semantics(repository):
    aware = datetime(2026, 8, 12, 14, 1, tzinfo=timezone.utc)
    naive = datetime(2026, 9, 11, 14, 1)
    vacancy = full_vacancy(published_at=aware, valid_through=naive)
    repository.insert_vacancy(vacancy)

    restored = repository.get_vacancy(vacancy)
    assert restored.published_at == aware
    assert restored.valid_through == naive


def test_structured_json_serialization_is_deterministic_unicode_safe_and_never_uses_repr():
    vacancy = full_vacancy(raw_source_metadata={"z": ["Львів"], "a": {"value": True}})

    first = vacancy_to_record(vacancy)
    second = vacancy_to_record(vacancy)
    assert first["raw_source_metadata_json"] == second["raw_source_metadata_json"]
    assert first["raw_source_metadata_json"] == '{"a":{"value":true},"z":["Львів"]}'
    assert "'" not in first["raw_source_metadata_json"]
    assert json.loads(first["raw_source_metadata_json"]) == {"a": {"value": True}, "z": ["Львів"]}


def test_same_source_and_source_id_is_inserted_once_even_with_different_discovery_profile(repository):
    original = full_vacancy(discovery_profiles=("python",))
    rediscovered = replace(original, discovery_profiles=("data_engineer",))

    assert repository.insert_vacancy(original).inserted is True
    assert repository.insert_vacancy(rediscovered).inserted is False
    assert repository.get_vacancy(original).discovery_profiles == ("python",)
    assert repository.connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 1


def test_missing_source_id_uses_source_and_url_as_temporary_duplicate_guard(repository):
    original = minimal_vacancy(source_vacancy_id=None)
    duplicate = replace(original, title="Changed title")

    assert repository.insert_vacancy(original).inserted is True
    assert repository.insert_vacancy(duplicate).inserted is False
    assert vacancy_identity_key(original) == vacancy_identity_key(duplicate)


def test_same_source_id_from_different_sources_is_distinct(repository):
    djinni = minimal_vacancy(source="djinni", source_vacancy_id="42")
    another_source = minimal_vacancy(source="another_source", source_vacancy_id="42")

    assert repository.insert_vacancy(djinni).inserted is True
    assert repository.insert_vacancy(another_source).inserted is True
    assert repository.connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 2


def test_incompatible_legacy_vacancies_schema_fails_clearly(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE vacancies (id INTEGER PRIMARY KEY, title TEXT, pub_date TEXT)")
    connection.commit()
    connection.close()

    repo = SQLiteVacancyRepository(db_path)
    with pytest.raises(RuntimeError, match="not canonical schema v1"):
        repo.open()


def test_canonical_repository_has_no_djinni_legacy_sql_columns_or_parser_dependency():
    source = (Path(__file__).parents[1] / "djinni_scraper" / "persistence" / "sqlite.py").read_text()

    assert "truncate_description" not in source
    assert "pub_date" not in source
    assert "views" not in source
    assert "responses" not in source
    assert "djinni_scraper.parsers" not in source

import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from djinni_scraper.domain.vacancy import Company, Compensation, LanguageRequirement, Location, Vacancy
from djinni_scraper.persistence import sqlite as sqlite_module
from djinni_scraper.persistence.sqlite import (
    IdentityConflictError,
    NotificationIntent,
    PersistenceStatus,
    SCHEMA_VERSION,
    SQLiteVacancyRepository,
    vacancy_content_hash,
    vacancy_identity_key,
    vacancy_to_record,
)


OBSERVED_AT = datetime(2026, 8, 13, 9, 30, tzinfo=timezone.utc)
LATER = OBSERVED_AT + timedelta(hours=1)
LATEST = OBSERVED_AT + timedelta(hours=2)


def minimal_vacancy(**overrides) -> Vacancy:
    values = {
        "source": "djinni",
        "source_vacancy_id": "842583",
        "url": "https://djinni.co/jobs/842583-role",
        "title": "Role",
        "scraped_at": OBSERVED_AT,
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
        "locations": (Location(country="Україна", city="Львів"),),
        "applicant_locations": (Location(region="Europe"),),
        "category": "AI/ML",
        "industry": "other",
        "tags": ("Product company", "AI"),
        "language_requirements": (LanguageRequirement(language="Англійська", level="B2"),),
        "published_at": date(2026, 8, 12),
        "valid_through": datetime(2026, 9, 11, 14, 1, 42),
        "scraped_at": OBSERVED_AT,
        "discovery_profiles": ("python",),
        "raw_source_metadata": {
            "jsonld": {"identifier": 842583},
            "site": {"publication_info": "196 views"},
        },
    }
    values.update(overrides)
    return Vacancy(**values)


@pytest.fixture
def repository(tmp_path):
    repo = SQLiteVacancyRepository(tmp_path / "nested" / "vacancies.db")
    repo.open()
    yield repo
    repo.close()


def persist(
    repository: SQLiteVacancyRepository,
    vacancy: Vacancy,
    *,
    observed_at: datetime = OBSERVED_AT,
    complete: bool = True,
    notification_intent: NotificationIntent | None = None,
):
    return repository.persist_observation(
        vacancy,
        observation_complete=complete,
        observed_at=observed_at,
        notification_intent=notification_intent,
    )


def _vacancy_row(repository, vacancy_id: int):
    return repository.connection.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()


def _vacancy_count(repository) -> int:
    return repository.connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0]


V1_SCHEMA = """
CREATE TABLE vacancies (
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
)
"""


def create_v1_database(path: Path, vacancy: Vacancy, profiles: tuple[str, ...]) -> None:
    record = vacancy_to_record(vacancy)
    record["identity_key"] = json.dumps(
        [vacancy.source, "source_vacancy_id", vacancy.source_vacancy_id],
        separators=(",", ":"),
    )
    record["discovery_profiles_json"] = json.dumps(profiles)
    fields = [
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
    ]
    connection = sqlite3.connect(path)
    try:
        connection.execute(V1_SCHEMA)
        connection.execute(
            f"INSERT INTO vacancies ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
            [record[field] for field in fields],
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()


def test_initialization_creates_schema_v4_with_publications_discoveries_outbox_and_foreign_keys(repository):
    connection = repository.connection
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    vacancy_columns = {row[1] for row in connection.execute("PRAGMA table_info(vacancies)")}
    discovery_columns = {row[1] for row in connection.execute("PRAGMA table_info(vacancy_discoveries)")}
    publication_columns = {row[1] for row in connection.execute("PRAGMA table_info(vacancy_publications)")}
    outbox_columns = {row[1] for row in connection.execute("PRAGMA table_info(notification_outbox)")}
    vacancy_indexes = {row[1] for row in connection.execute("PRAGMA index_list(vacancies)")}
    discovery_foreign_keys = list(connection.execute("PRAGMA foreign_key_list(vacancy_discoveries)"))

    assert {"vacancies", "vacancy_publications", "vacancy_discoveries", "notification_outbox"}.issubset(tables)
    assert {"content_hash", "first_seen_at", "last_seen_at"}.issubset(vacancy_columns)
    assert "discovery_profiles_json" not in vacancy_columns
    assert _source_transport_only_columns().isdisjoint(vacancy_columns)
    assert {"vacancy_id", "profile_slug", "first_discovered_at", "last_discovered_at"} == discovery_columns
    assert {"id", "vacancy_id", "published_at", "first_observed_at", "last_observed_at"} == publication_columns
    assert {"vacancy_id", "publication_id", "event_type", "event_key", "route", "attempt_count", "next_attempt_at", "delivered_at"}.issubset(outbox_columns)
    assert {"idx_vacancies_source", "idx_vacancies_source_vacancy_id", "idx_vacancies_published_at", "idx_vacancies_last_seen_at"}.issubset(vacancy_indexes)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert discovery_foreign_keys[0][2:] == ("vacancies", "vacancy_id", "id", "NO ACTION", "CASCADE", "NONE")


def test_v1_database_migrates_preserving_rows_backfills_discovery_hash_and_timestamps(tmp_path):
    db_path = tmp_path / "v1.db"
    legacy = full_vacancy(discovery_profiles=("python", "data_engineer"))
    create_v1_database(db_path, legacy, legacy.discovery_profiles)

    repository = SQLiteVacancyRepository(db_path)
    repository.open()
    row = _vacancy_row(repository, 1)
    restored = repository.get_vacancy_by_identity_key(vacancy_identity_key(legacy))

    assert repository.connection.execute("PRAGMA user_version").fetchone()[0] == 4
    assert _vacancy_count(repository) == 1
    assert restored == replace(legacy, discovery_profiles=("data_engineer", "python"))
    assert row["content_hash"] == vacancy_content_hash(legacy)
    assert row["first_seen_at"] == OBSERVED_AT.isoformat()
    assert row["last_seen_at"] == OBSERVED_AT.isoformat()
    assert repository.get_discovery_profiles(1) == ("data_engineer", "python")
    assert [publication.published_at for publication in repository.list_publications(1)] == ["2026-08-12"]
    assert repository.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0
    assert "discovery_profiles_json" not in {entry[1] for entry in repository.connection.execute("PRAGMA table_info(vacancies)")}

    repository.close()
    repository.open()
    assert repository.connection.execute("PRAGMA user_version").fetchone()[0] == 4
    assert _vacancy_count(repository) == 1
    repository.close()


def test_failed_v1_migration_rolls_back_table_and_user_version(tmp_path, monkeypatch):
    db_path = tmp_path / "v1-failure.db"
    create_v1_database(db_path, minimal_vacancy(discovery_profiles=("python",)), ("python",))
    monkeypatch.setattr(
        sqlite_module,
        "_create_discoveries_table",
        lambda _connection: (_ for _ in ()).throw(sqlite3.OperationalError("forced migration failure")),
    )

    with pytest.raises(sqlite3.OperationalError, match="forced migration failure"):
        SQLiteVacancyRepository(db_path).open()

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 1
        assert "discovery_profiles_json" in {row[1] for row in connection.execute("PRAGMA table_info(vacancies)")}
    finally:
        connection.close()


def test_unsupported_future_schema_is_rejected(tmp_path):
    db_path = tmp_path / "future.db"
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA user_version = 5")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="Unsupported SQLite schema version 5"):
        SQLiteVacancyRepository(db_path).open()


def test_new_stable_identity_inserts_once_and_duplicate_is_unchanged(repository):
    vacancy = full_vacancy(discovery_profiles=("python",))

    first = persist(repository, vacancy)
    second = persist(repository, replace(vacancy, scraped_at=LATER), observed_at=LATER)

    assert first.status is PersistenceStatus.INSERTED
    assert first.discovery_added is True
    assert second.status is PersistenceStatus.UNCHANGED
    assert second.discovery_added is False
    assert _vacancy_count(repository) == 1


def test_different_profile_merges_discovery_without_duplicate_or_content_update(repository):
    vacancy = full_vacancy(discovery_profiles=("python",))
    first = persist(repository, vacancy)
    second = persist(
        repository,
        replace(vacancy, discovery_profiles=("data_engineer",), scraped_at=LATER),
        observed_at=LATER,
    )

    assert second.status is PersistenceStatus.UNCHANGED
    assert second.discovery_added is True
    assert _vacancy_count(repository) == 1
    assert repository.get_discovery_profiles(first.vacancy_id) == ("data_engineer", "python")


def test_fallback_identity_normalizes_trailing_slash_query_and_fragment(repository):
    initial = minimal_vacancy(source_vacancy_id=None, url="https://djinni.co/jobs/842583-role/")
    repeat = replace(initial, url="https://DJINNI.CO/jobs/842583-role/?utm_source=test#details", scraped_at=LATER)

    first = persist(repository, initial)
    second = persist(repository, repeat, observed_at=LATER)

    assert first.status is PersistenceStatus.INSERTED
    assert second.status is PersistenceStatus.UNCHANGED
    assert vacancy_identity_key(initial) == vacancy_identity_key(repeat)
    assert _vacancy_count(repository) == 1


def test_url_fallback_is_promoted_to_stable_source_id_without_duplicate(repository):
    fallback = full_vacancy(source_vacancy_id=None, discovery_profiles=("python",))
    first = persist(repository, fallback)
    stable = replace(fallback, source_vacancy_id="842583", scraped_at=LATER)

    promoted = persist(repository, stable, observed_at=LATER)
    row = _vacancy_row(repository, first.vacancy_id)

    assert promoted.status is PersistenceStatus.UNCHANGED
    assert promoted.vacancy_id == first.vacancy_id
    assert _vacancy_count(repository) == 1
    assert row["source_vacancy_id"] == "842583"
    assert row["identity_key"] == vacancy_identity_key(stable)
    assert repository.get_vacancy(stable).source_vacancy_id == "842583"


def test_same_source_id_with_new_slug_updates_one_canonical_row(repository):
    original = minimal_vacancy(url="https://djinni.co/jobs/842583-old-slug")
    first = persist(repository, original)
    changed_url = replace(original, url="https://djinni.co/jobs/842583-new-slug", scraped_at=LATER)

    result = persist(repository, changed_url, observed_at=LATER)

    assert result.status is PersistenceStatus.UPDATED
    assert result.vacancy_id == first.vacancy_id
    assert _vacancy_count(repository) == 1
    assert repository.get_vacancy(changed_url).url.endswith("new-slug")


def test_same_url_from_different_sources_remains_distinct(repository):
    djinni = minimal_vacancy(source="djinni", source_vacancy_id=None)
    other_source = minimal_vacancy(source="dou", source_vacancy_id=None)

    persist(repository, djinni)
    persist(repository, other_source)

    assert _vacancy_count(repository) == 2


def test_conflicting_stable_id_and_fallback_rows_fail_without_merging(repository):
    fallback = minimal_vacancy(source_vacancy_id=None, url="https://djinni.co/jobs/42-fallback")
    stable = minimal_vacancy(source_vacancy_id="42", url="https://djinni.co/jobs/42-stable")
    persist(repository, fallback)
    persist(repository, stable)

    with pytest.raises(IdentityConflictError, match="different canonical vacancies"):
        persist(repository, replace(stable, url=fallback.url), observed_at=LATER)

    assert _vacancy_count(repository) == 2


def test_title_and_published_date_are_not_identity_even_when_content_updates(repository):
    original = full_vacancy()
    first = persist(repository, original)
    changed = replace(original, title="Renamed role", published_at=date(2026, 8, 14), scraped_at=LATER)

    result = persist(repository, changed, observed_at=LATER)

    assert result.status is PersistenceStatus.REPUBLISHED
    assert result.vacancy_id == first.vacancy_id
    assert _vacancy_count(repository) == 1


def test_initial_publication_is_created_only_for_non_null_published_at(repository):
    with_date = persist(repository, full_vacancy())
    without_date = persist(
        repository,
        minimal_vacancy(source_vacancy_id="without-date", published_at=None),
        observed_at=LATER,
    )

    assert [publication.published_at for publication in repository.list_publications(with_date.vacancy_id)] == [
        "2026-08-12"
    ]
    assert repository.list_publications(without_date.vacancy_id) == ()


def test_same_publication_date_is_unchanged_but_content_change_is_updated(repository):
    original = full_vacancy()
    first = persist(repository, original)
    same_date = persist(repository, replace(original, scraped_at=LATER), observed_at=LATER)
    changed = persist(
        repository,
        replace(original, title="Changed title", scraped_at=LATEST),
        observed_at=LATEST,
    )

    assert same_date.status is PersistenceStatus.UNCHANGED
    assert changed.status is PersistenceStatus.UPDATED
    assert len(repository.list_publications(first.vacancy_id)) == 1


def test_newer_publication_returns_republished_keeps_identity_and_refreshes_complete_snapshot(repository):
    original = full_vacancy()
    first = persist(repository, original)
    repost = replace(
        original,
        published_at=date(2026, 8, 15),
        title="Republished title",
        scraped_at=LATER,
    )

    result = persist(repository, repost, observed_at=LATER)

    assert result.status is PersistenceStatus.REPUBLISHED
    assert result.vacancy_id == first.vacancy_id
    assert result.publication_added is True
    assert repository.get_vacancy(repost) == repost
    assert [publication.published_at for publication in repository.list_publications(first.vacancy_id)] == [
        "2026-08-12",
        "2026-08-15",
    ]


def test_publication_date_alone_does_not_create_content_update_but_is_republished(repository):
    original = full_vacancy()
    first = persist(repository, original)
    repost = replace(original, published_at=date(2026, 8, 15), scraped_at=LATER)

    result = persist(repository, repost, observed_at=LATER)

    assert vacancy_content_hash(original) == vacancy_content_hash(repost)
    assert result.status is PersistenceStatus.REPUBLISHED
    assert result.vacancy_id == first.vacancy_id


@pytest.mark.parametrize(
    "published_at",
    (
        date(2026, 8, 12),
        datetime(2026, 8, 12, 14, 1, 42, 527603),
        datetime(2026, 8, 12, 14, 1, 42, 527603, tzinfo=timezone.utc),
    ),
)
def test_publication_day_normalization_preserves_canonical_source_precision(repository, published_at):
    source = full_vacancy(published_at=published_at)
    result = persist(repository, source)
    publication = repository.list_publications(result.vacancy_id)[0]

    assert publication.published_at == "2026-08-12"
    assert repository.get_vacancy(source).published_at == published_at


def test_same_day_timestamp_change_is_not_republished(repository):
    original = full_vacancy(published_at=datetime(2026, 8, 12, 9, 0, 0, 100))
    first = persist(
        repository,
        original,
        notification_intent=NotificationIntent(route="main", profile_slug="python"),
    )
    same_publication = replace(
        original,
        published_at=datetime(2026, 8, 12, 14, 1, 42, 527603),
        scraped_at=LATER,
    )

    result = persist(
        repository,
        same_publication,
        observed_at=LATER,
        notification_intent=NotificationIntent(route="main", profile_slug="python"),
    )

    assert result.status is PersistenceStatus.UNCHANGED
    assert result.vacancy_id == first.vacancy_id
    assert result.publication_added is False
    assert result.notification_enqueued is False
    assert [publication.published_at for publication in repository.list_publications(first.vacancy_id)] == [
        "2026-08-12",
    ]
    assert repository.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1


def test_date_and_midnight_datetime_representations_share_one_publication_day(repository):
    original = full_vacancy(published_at=date(2026, 8, 12))
    first = persist(repository, original)
    midnight_naive = persist(
        repository,
        replace(original, published_at=datetime(2026, 8, 12), scraped_at=LATER),
        observed_at=LATER,
    )
    midnight_utc = persist(
        repository,
        replace(
            original,
            published_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
            scraped_at=LATEST,
        ),
        observed_at=LATEST,
    )

    assert midnight_naive.status is PersistenceStatus.UNCHANGED
    assert midnight_utc.status is PersistenceStatus.UNCHANGED
    assert len(repository.list_publications(first.vacancy_id)) == 1


def test_equivalent_timezone_instants_share_one_publication_day(repository):
    original = full_vacancy(
        published_at=datetime(2026, 8, 12, 10, 0, tzinfo=timezone(timedelta(hours=2)))
    )
    first = persist(repository, original)
    same_instant = replace(
        original,
        published_at=datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc),
        scraped_at=LATER,
    )

    result = persist(repository, same_instant, observed_at=LATER)

    assert result.status is PersistenceStatus.UNCHANGED
    assert len(repository.list_publications(first.vacancy_id)) == 1


def test_next_publication_day_is_republished_with_a_new_event(repository):
    original = full_vacancy(published_at=datetime(2026, 8, 12, 14, 0))
    first = persist(repository, original)
    repost = replace(original, published_at=datetime(2026, 8, 13, 9, 0), scraped_at=LATER)

    result = persist(
        repository,
        repost,
        observed_at=LATER,
        notification_intent=NotificationIntent(route="main", profile_slug="python"),
    )

    assert result.status is PersistenceStatus.REPUBLISHED
    assert result.notification_enqueued is True
    assert [publication.published_at for publication in repository.list_publications(first.vacancy_id)] == [
        "2026-08-12",
        "2026-08-13",
    ]


def test_first_known_publication_for_existing_historyless_vacancy_is_baseline_not_repost(repository):
    original = full_vacancy(published_at=None)
    first = persist(repository, original)
    date_now_known = replace(original, published_at=date(2026, 8, 12), scraped_at=LATER)

    result = persist(repository, date_now_known, observed_at=LATER)

    assert result.status is PersistenceStatus.UPDATED
    assert result.vacancy_id == first.vacancy_id
    assert result.publication_added is True
    assert [publication.published_at for publication in repository.list_publications(first.vacancy_id)] == [
        "2026-08-12"
    ]


def test_older_unseen_publication_is_recorded_without_republish_or_snapshot_regression(repository):
    current = full_vacancy(published_at=date(2026, 8, 15), title="Current title")
    first = persist(repository, current)
    older = replace(
        current,
        published_at=date(2026, 8, 12),
        title="Historical title",
        scraped_at=LATER,
    )

    result = persist(repository, older, observed_at=LATER)

    assert result.status is PersistenceStatus.UNCHANGED
    assert result.publication_added is True
    assert repository.get_vacancy(current).title == "Current title"
    assert [publication.published_at for publication in repository.list_publications(first.vacancy_id)] == [
        "2026-08-12",
        "2026-08-15",
    ]


def test_none_published_at_does_not_change_publication_history(repository):
    first = persist(repository, full_vacancy())
    result = persist(
        repository,
        replace(full_vacancy(), published_at=None, scraped_at=LATER),
        observed_at=LATER,
        complete=False,
    )

    assert result.status is PersistenceStatus.UNCHANGED
    assert result.publication_added is False
    assert len(repository.list_publications(first.vacancy_id)) == 1


def test_incomplete_republication_records_history_without_erasing_complete_snapshot(repository):
    complete = full_vacancy()
    first = persist(repository, complete)
    listing_only_repost = replace(
        complete,
        published_at=date(2026, 8, 15),
        title="Listing-only title",
        description="Listing-only description",
        scraped_at=LATER,
    )

    result = persist(
        repository,
        listing_only_repost,
        observed_at=LATER,
        complete=False,
        notification_intent=NotificationIntent(route="main", profile_slug="python"),
    )

    assert result.status is PersistenceStatus.REPUBLISHED
    assert result.notification_enqueued is True
    assert repository.get_vacancy(complete).title == complete.title
    assert len(repository.list_publications(first.vacancy_id)) == 2


@pytest.mark.parametrize(
    "changed",
    [
        lambda vacancy: replace(vacancy, title="New title"),
        lambda vacancy: replace(vacancy, description="New description"),
        lambda vacancy: replace(vacancy, compensation=Compensation(max_amount=3000, currency="USD")),
        lambda vacancy: replace(vacancy, category="Data"),
        lambda vacancy: replace(vacancy, tags=("Changed",)),
        lambda vacancy: replace(vacancy, locations=(Location(country="Poland", city="Warsaw"),)),
    ],
)
def test_complete_canonical_content_changes_replace_current_snapshot(repository, changed):
    original = full_vacancy()
    first = persist(repository, original)
    incoming = replace(changed(original), scraped_at=LATER)

    result = persist(repository, incoming, observed_at=LATER)
    row = _vacancy_row(repository, first.vacancy_id)

    assert result.status is PersistenceStatus.UPDATED
    assert row["first_seen_at"] == OBSERVED_AT.isoformat()
    assert row["last_seen_at"] == LATER.isoformat()
    assert repository.get_vacancy(incoming) == incoming


def test_scraped_at_and_raw_metadata_alone_do_not_create_content_update(repository):
    original = full_vacancy()
    first = persist(repository, original)
    noisy = replace(
        original,
        scraped_at=LATER,
        raw_source_metadata={"site": {"publication_info": "999 views"}},
    )

    result = persist(repository, noisy, observed_at=LATER)
    row = _vacancy_row(repository, first.vacancy_id)

    assert result.status is PersistenceStatus.UNCHANGED
    assert row["scraped_at"] == OBSERVED_AT.isoformat()
    assert row["raw_source_metadata_json"] == vacancy_to_record(original)["raw_source_metadata_json"]
    assert row["last_seen_at"] == LATER.isoformat()
    assert vacancy_content_hash(original) == vacancy_content_hash(noisy)


def test_complete_snapshot_can_clear_an_optional_field_when_source_reports_it_missing(repository):
    original = full_vacancy()
    first = persist(repository, original)
    missing_salary = replace(original, compensation=None, scraped_at=LATER)

    result = persist(repository, missing_salary, observed_at=LATER)

    assert result.status is PersistenceStatus.UPDATED
    assert repository.get_vacancy(missing_salary).compensation is None
    assert _vacancy_row(repository, first.vacancy_id)["last_seen_at"] == LATER.isoformat()


def test_content_hash_is_deterministic_and_excludes_identity_and_discovery(repository):
    original = full_vacancy(discovery_profiles=("python",))
    equivalent = replace(
        original,
        source_vacancy_id="different-id",
        discovery_profiles=("data_engineer",),
        published_at=date(2026, 8, 14),
        scraped_at=LATER,
    )

    assert vacancy_content_hash(original) == vacancy_content_hash(original)
    assert vacancy_content_hash(original) == vacancy_content_hash(equivalent)
    assert vacancy_to_record(original)["content_hash"] == vacancy_content_hash(original)


def test_repeat_profile_preserves_first_discovered_and_updates_last_discovered(repository):
    first = persist(repository, full_vacancy(discovery_profiles=("python",)))
    second = persist(
        repository,
        full_vacancy(discovery_profiles=("python",), scraped_at=LATER),
        observed_at=LATER,
    )
    discovery = repository.get_discoveries(first.vacancy_id)[0]

    assert second.status is PersistenceStatus.UNCHANGED
    assert second.discovery_added is False
    assert discovery.first_discovered_at == OBSERVED_AT
    assert discovery.last_discovered_at == LATER


def test_discovery_profiles_are_deterministic_and_vacancy_is_not_duplicated(repository):
    first = persist(repository, full_vacancy(discovery_profiles=("support", "python")))
    persist(
        repository,
        full_vacancy(discovery_profiles=("data_engineer", "python"), scraped_at=LATER),
        observed_at=LATER,
    )

    assert repository.get_discovery_profiles(first.vacancy_id) == ("data_engineer", "python", "support")
    assert _vacancy_count(repository) == 1


def test_incomplete_observation_does_not_erase_full_canonical_snapshot(repository):
    full = full_vacancy(discovery_profiles=("python",))
    first = persist(repository, full)
    degraded = replace(
        full,
        description="listing-only description",
        compensation=None,
        company=None,
        locations=(),
        discovery_profiles=("other",),
        scraped_at=LATER,
    )

    result = persist(repository, degraded, observed_at=LATER, complete=False)
    restored = repository.get_vacancy(full)

    assert result.status is PersistenceStatus.UNCHANGED
    assert result.discovery_added is True
    assert restored.description == full.description
    assert restored.compensation == full.compensation
    assert restored.company == full.company
    assert restored.locations == full.locations
    assert repository.get_discovery_profiles(first.vacancy_id) == ("other", "python")


def test_incomplete_new_observation_inserts_and_later_complete_snapshot_enriches(repository):
    degraded = minimal_vacancy(source_vacancy_id="new", title="Listing title", discovery_profiles=("python",))
    first = persist(repository, degraded, complete=False)
    complete = replace(
        degraded,
        title="Detail title",
        description="Complete description",
        company=Company(name="Company"),
        scraped_at=LATER,
    )

    enriched = persist(repository, complete, observed_at=LATER, complete=True)

    assert first.status is PersistenceStatus.INSERTED
    assert enriched.status is PersistenceStatus.UPDATED
    assert repository.get_vacancy(complete) == complete


def test_discovery_failure_rolls_back_vacancy_insert(repository, monkeypatch):
    monkeypatch.setattr(
        repository,
        "_merge_discoveries",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("discovery unavailable")),
    )

    with pytest.raises(sqlite3.OperationalError, match="discovery unavailable"):
        persist(repository, full_vacancy())

    assert _vacancy_count(repository) == 0
    assert repository.connection.execute("SELECT COUNT(*) FROM vacancy_discoveries").fetchone()[0] == 0


def test_vacancy_update_failure_does_not_modify_discovery(repository, monkeypatch):
    original = full_vacancy(discovery_profiles=("python",))
    first = persist(repository, original)
    monkeypatch.setattr(
        repository,
        "_update_vacancy_row",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("update unavailable")),
    )

    with pytest.raises(sqlite3.OperationalError, match="update unavailable"):
        persist(
            repository,
            replace(original, title="Changed", discovery_profiles=("data_engineer",), scraped_at=LATER),
            observed_at=LATER,
        )

    assert repository.get_vacancy(original).title == original.title
    assert repository.get_discovery_profiles(first.vacancy_id) == ("python",)


def test_foreign_key_integrity_and_cascade_are_active(repository):
    result = persist(repository, full_vacancy())
    connection = repository.connection

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO vacancy_discoveries (
                vacancy_id, profile_slug, first_discovered_at, last_discovered_at
            ) VALUES (999, 'python', ?, ?)
            """,
            (OBSERVED_AT.isoformat(), OBSERVED_AT.isoformat()),
        )
    connection.rollback()
    connection.execute("DELETE FROM vacancies WHERE id = ?", (result.vacancy_id,))
    connection.commit()

    assert connection.execute("SELECT COUNT(*) FROM vacancy_discoveries").fetchone()[0] == 0


def test_repository_creates_missing_parent_directories_and_round_trips_full_values(tmp_path):
    db_path = tmp_path / "custom" / "state" / "vacancies.db"
    repo = SQLiteVacancyRepository(db_path)
    repo.open()
    vacancy = full_vacancy()

    result = persist(repo, vacancy)

    assert db_path.exists()
    assert result.status is PersistenceStatus.INSERTED
    assert repo.get_vacancy(vacancy) == vacancy
    repo.close()


def _source_transport_only_columns() -> set[str]:
    return {"listing_description", "listing_published_at", "raw_jsonld"}

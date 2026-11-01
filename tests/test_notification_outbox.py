import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from djinni_scraper.domain.vacancy import Company, Compensation, LanguageRequirement, Location, Vacancy
from djinni_scraper.notifications.dispatcher import NotificationDispatcher
from djinni_scraper.notifications.routing import (
    NotificationConfigurationError,
    TelegramDestination,
    TelegramRouteResolver,
    notification_context_from_spider,
)
from djinni_scraper.notifications.telegram import (
    PARSE_MODE,
    TelegramClient,
    TelegramDeliveryError,
    format_vacancy_notification,
)
from djinni_scraper.persistence import sqlite as sqlite_module
from djinni_scraper.persistence.sqlite import (
    NotificationIntent,
    PersistenceStatus,
    SQLiteVacancyRepository,
    vacancy_to_record,
)
from djinni_scraper.search_profiles import get_search_profile


NOW = datetime(2026, 8, 13, 9, 30, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=15)


class RecordingTelegramClient:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = []

    def send(self, destination, message):
        self.calls.append((destination, message))
        if self.error is not None:
            raise self.error


def vacancy(**overrides) -> Vacancy:
    values = {
        "source": "djinni",
        "source_vacancy_id": "842583",
        "url": "https://djinni.co/jobs/842583-role",
        "title": "Canonical role",
        "description": "A full canonical description.",
        "company": Company(name="Softcery"),
        "compensation": Compensation(min_amount=1200, max_amount=2000, currency="USD", unit="MONTH"),
        "experience_months": 12,
        "locations": (Location(country="Україна", city="Львів"),),
        "applicant_locations": (Location(region="Europe"),),
        "category": "Python",
        "tags": ("AI", "Remote"),
        "language_requirements": (LanguageRequirement(language="English", level="B2"),),
        "published_at": date(2026, 8, 12),
        "scraped_at": NOW,
        "discovery_profiles": ("python",),
    }
    values.update(overrides)
    return Vacancy(**values)


@pytest.fixture
def repository(tmp_path):
    repo = SQLiteVacancyRepository(tmp_path / "vacancies.db")
    repo.open()
    yield repo
    repo.close()


@pytest.fixture
def resolver():
    return TelegramRouteResolver(
        {
            "main": TelegramDestination(token="main-token", chat_id="main-chat"),
            "alternate": TelegramDestination(token="alternate-token", chat_id="alternate-chat"),
        }
    )


def persist_new(repository, *, route="main", profile_slug="python", observed_at=NOW):
    return repository.persist_observation(
        vacancy(),
        observation_complete=True,
        observed_at=observed_at,
        notification_intent=NotificationIntent(route=route, profile_slug=profile_slug),
    )


V2_SCHEMA = """
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
    raw_source_metadata_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE vacancy_discoveries (
    vacancy_id INTEGER NOT NULL,
    profile_slug TEXT NOT NULL,
    first_discovered_at TEXT NOT NULL,
    last_discovered_at TEXT NOT NULL,
    PRIMARY KEY (vacancy_id, profile_slug),
    FOREIGN KEY (vacancy_id) REFERENCES vacancies(id) ON DELETE CASCADE
) WITHOUT ROWID;
"""


def create_v2_database(path: Path) -> None:
    record = vacancy_to_record(vacancy())
    fields = tuple(record)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(V2_SCHEMA)
        connection.execute("CREATE INDEX idx_vacancies_source ON vacancies (source)")
        connection.execute(
            "CREATE INDEX idx_vacancies_source_vacancy_id ON vacancies (source, source_vacancy_id)"
        )
        connection.execute("CREATE INDEX idx_vacancies_published_at ON vacancies (published_at)")
        connection.execute("CREATE INDEX idx_vacancies_last_seen_at ON vacancies (last_seen_at)")
        connection.execute(
            "CREATE INDEX idx_vacancy_discoveries_profile_slug ON vacancy_discoveries (profile_slug)"
        )
        connection.execute(
            f"INSERT INTO vacancies ({', '.join(fields)}, first_seen_at, last_seen_at) "
            f"VALUES ({', '.join('?' for _ in fields)}, ?, ?)",
            [*([record[field] for field in fields]), NOW.isoformat(), NOW.isoformat()],
        )
        connection.execute(
            """
            INSERT INTO vacancy_discoveries (
                vacancy_id, profile_slug, first_discovered_at, last_discovered_at
            ) VALUES (1, 'python', ?, ?)
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
    finally:
        connection.close()


def test_v2_to_v3_migration_preserves_rows_discoveries_and_does_not_backfill_notifications(tmp_path):
    db_path = tmp_path / "v2.db"
    create_v2_database(db_path)

    repo = SQLiteVacancyRepository(db_path)
    repo.open()

    assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == 4
    assert repo.connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 1
    assert repo.get_discovery_profiles(1) == ("python",)
    assert [publication.published_at for publication in repo.list_publications(1)] == ["2026-08-12"]
    assert repo.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0
    repo.close()
    repo.open()
    assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == 4
    repo.close()


def test_v2_to_v3_migration_failure_rolls_back_without_dropping_v2_state(tmp_path, monkeypatch):
    db_path = tmp_path / "v2-failure.db"
    create_v2_database(db_path)
    monkeypatch.setattr(
        sqlite_module,
        "_create_v3_outbox_table",
        lambda _connection: (_ for _ in ()).throw(sqlite3.OperationalError("outbox migration failed")),
    )

    with pytest.raises(sqlite3.OperationalError, match="outbox migration failed"):
        SQLiteVacancyRepository(db_path).open()

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM vacancy_discoveries").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'notification_outbox'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


V3_OUTBOX_SCHEMA = """
CREATE TABLE notification_outbox (
    id INTEGER PRIMARY KEY,
    vacancy_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    route TEXT NOT NULL,
    profile_slug TEXT,
    created_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    next_attempt_at TEXT,
    delivered_at TEXT,
    last_error TEXT,
    UNIQUE (vacancy_id, event_type),
    FOREIGN KEY (vacancy_id) REFERENCES vacancies(id) ON DELETE CASCADE
)
"""


def create_v3_database(path: Path) -> None:
    create_v2_database(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute(V3_OUTBOX_SCHEMA)
        connection.execute(
            """
            INSERT INTO notification_outbox (
                id, vacancy_id, event_type, route, profile_slug, created_at, attempt_count,
                last_attempt_at, next_attempt_at, delivered_at, last_error
            ) VALUES (7, 1, 'VACANCY_INSERTED', 'main', 'python', ?, 2, ?, NULL, ?, NULL)
            """,
            (NOW.isoformat(), NOW.isoformat(), LATER.isoformat()),
        )
        connection.execute("UPDATE vacancies SET content_hash = 'obsolete-v3-hash' WHERE id = 1")
        connection.execute(
            """
            CREATE INDEX idx_notification_outbox_pending
            ON notification_outbox (delivered_at, next_attempt_at, created_at, id)
            """
        )
        connection.execute(
            "CREATE INDEX idx_notification_outbox_vacancy_id ON notification_outbox (vacancy_id)"
        )
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
    finally:
        connection.close()


def test_v3_to_v4_migration_preserves_state_backfills_publications_and_rekeys_outbox(tmp_path):
    db_path = tmp_path / "v3.db"
    create_v3_database(db_path)

    repo = SQLiteVacancyRepository(db_path)
    repo.open()
    event = repo.get_notification_event(7)

    assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == 4
    assert repo.connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 1
    assert repo.get_discovery_profiles(1) == ("python",)
    assert [publication.published_at for publication in repo.list_publications(1)] == ["2026-08-12"]
    assert event.id == 7
    assert event.event_key == "vacancy:1:inserted"
    assert event.publication_id is None
    assert event.attempt_count == 2
    assert event.delivered_at == LATER
    assert repo.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
    assert {
        "idx_notification_outbox_pending",
        "idx_notification_outbox_vacancy_id",
    }.issubset({row[1] for row in repo.connection.execute("PRAGMA index_list(notification_outbox)")})
    assert repo.connection.execute("SELECT content_hash FROM vacancies WHERE id = 1").fetchone()[0] == vacancy_to_record(vacancy())["content_hash"]

    repo.close()
    repo.open()
    assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == 4
    assert repo.connection.execute("SELECT COUNT(*) FROM vacancy_publications").fetchone()[0] == 1
    repo.close()


def test_v3_to_v4_failure_rolls_back_to_valid_v3(tmp_path, monkeypatch):
    db_path = tmp_path / "v3-failure.db"
    create_v3_database(db_path)
    monkeypatch.setattr(
        sqlite_module,
        "_create_outbox_table",
        lambda _connection: (_ for _ in ()).throw(sqlite3.OperationalError("v4 outbox migration failed")),
    )

    with pytest.raises(sqlite3.OperationalError, match="v4 outbox migration failed"):
        SQLiteVacancyRepository(db_path).open()

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM vacancy_discoveries").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'vacancy_publications'").fetchone()[0] == 0
    finally:
        connection.close()


def test_v3_migration_baseline_matches_equivalent_runtime_publication_day(tmp_path):
    db_path = tmp_path / "v3-equivalent-publication.db"
    create_v3_database(db_path)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("UPDATE vacancies SET published_at = ? WHERE id = 1", ("2026-11-15T10:00:00+02:00",))
        connection.commit()
    finally:
        connection.close()

    repo = SQLiteVacancyRepository(db_path)
    repo.open()
    result = repo.persist_observation(
        replace(
            vacancy(),
            published_at=datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc),
            scraped_at=LATER,
        ),
        observation_complete=True,
        observed_at=LATER,
        notification_intent=NotificationIntent(route="main", profile_slug="python"),
    )

    assert [publication.published_at for publication in repo.list_publications(1)] == ["2026-11-15"]
    assert result.status is PersistenceStatus.UNCHANGED
    assert result.notification_enqueued is False
    assert repo.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
    repo.close()


def test_inserted_creates_one_atomic_outbox_event_with_route_and_profile(repository):
    result = persist_new(repository, route="alternate", profile_slug="support")
    event = repository.get_notification_event(result.notification_event_id)

    assert result.status is PersistenceStatus.INSERTED
    assert result.notification_enqueued is True
    assert event.event_type == "VACANCY_INSERTED"
    assert event.publication_id is None
    assert event.event_key == f"vacancy:{result.vacancy_id}:inserted"
    assert event.route == "alternate"
    assert event.profile_slug == "support"
    assert event.attempt_count == 0
    assert event.delivered_at is None


def test_existing_updates_unchanged_and_discovery_only_never_create_a_second_event(repository):
    first = persist_new(repository)
    current_snapshot = replace(vacancy(), title="Updated title", scraped_at=LATER)
    updated = repository.persist_observation(
        current_snapshot,
        observation_complete=True,
        observed_at=LATER,
        notification_intent=NotificationIntent(route="alternate", profile_slug="support"),
    )
    discovery_only = repository.persist_observation(
        replace(current_snapshot, discovery_profiles=("data_engineer",)),
        observation_complete=True,
        observed_at=LATER,
        notification_intent=NotificationIntent(route="alternate", profile_slug="data_engineer"),
    )

    assert first.status is PersistenceStatus.INSERTED
    assert updated.status is PersistenceStatus.UPDATED
    assert discovery_only.status is PersistenceStatus.UNCHANGED
    assert repository.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
    assert repository.get_notification_event(first.notification_event_id).route == "main"


def test_republished_creates_one_distinct_event_with_publication_identity(repository):
    first = persist_new(repository)
    repost = repository.persist_observation(
        replace(vacancy(), published_at=date(2026, 8, 15), scraped_at=LATER),
        observation_complete=True,
        observed_at=LATER,
        notification_intent=NotificationIntent(route="alternate", profile_slug="support"),
    )
    event = repository.get_notification_event(repost.notification_event_id)

    assert repost.status is PersistenceStatus.REPUBLISHED
    assert repost.vacancy_id == first.vacancy_id
    assert repost.notification_enqueued is True
    assert event.event_type == "VACANCY_REPUBLISHED"
    assert event.publication_id == repost.publication_id
    assert event.event_key == f"publication:{repost.publication_id}:republished"
    assert event.route == "alternate"
    assert event.profile_slug == "support"


def test_same_repost_from_another_profile_creates_no_second_publication_or_event(repository):
    first = persist_new(repository)
    repost = replace(vacancy(), published_at=date(2026, 8, 15), scraped_at=LATER)
    first_repost = repository.persist_observation(
        repost,
        observation_complete=True,
        observed_at=LATER,
        notification_intent=NotificationIntent(route="main", profile_slug="python"),
    )
    second_repost = repository.persist_observation(
        replace(repost, discovery_profiles=("data_engineer",)),
        observation_complete=True,
        observed_at=LATER,
        notification_intent=NotificationIntent(route="main", profile_slug="data_engineer"),
    )

    assert first_repost.status is PersistenceStatus.REPUBLISHED
    assert second_repost.status is PersistenceStatus.UNCHANGED
    assert second_repost.notification_enqueued is False
    assert len(repository.list_publications(first.vacancy_id)) == 2
    assert repository.get_discovery_profiles(first.vacancy_id) == ("data_engineer", "python")
    assert repository.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 2


def test_multiple_reposts_create_one_logical_vacancy_and_three_distinct_events(repository):
    first = persist_new(repository)
    repost_one = repository.persist_observation(
        replace(vacancy(), published_at=date(2026, 8, 15), scraped_at=LATER),
        observation_complete=True,
        observed_at=LATER,
        notification_intent=NotificationIntent(route="main", profile_slug="python"),
    )
    repost_two = repository.persist_observation(
        replace(vacancy(), published_at=date(2026, 9, 1), scraped_at=LATER + timedelta(minutes=1)),
        observation_complete=True,
        observed_at=LATER + timedelta(minutes=1),
        notification_intent=NotificationIntent(route="main", profile_slug="python"),
    )
    events = tuple(
        tuple(row)
        for row in repository.connection.execute(
            "SELECT event_type, event_key FROM notification_outbox ORDER BY id"
        )
    )

    assert repost_one.status is PersistenceStatus.REPUBLISHED
    assert repost_two.status is PersistenceStatus.REPUBLISHED
    assert repository.connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 1
    assert len(repository.list_publications(first.vacancy_id)) == 3
    assert events == (
        ("VACANCY_INSERTED", "vacancy:1:inserted"),
        ("VACANCY_REPUBLISHED", f"publication:{repost_one.publication_id}:republished"),
        ("VACANCY_REPUBLISHED", f"publication:{repost_two.publication_id}:republished"),
    )


def test_outbox_failure_rolls_back_vacancy_and_discovery_insert(repository, monkeypatch):
    monkeypatch.setattr(
        repository,
        "_enqueue_notification",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("outbox unavailable")),
    )

    with pytest.raises(sqlite3.OperationalError, match="outbox unavailable"):
        persist_new(repository)

    assert repository.connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 0
    assert repository.connection.execute("SELECT COUNT(*) FROM vacancy_discoveries").fetchone()[0] == 0
    assert repository.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0


def test_dispatch_success_marks_delivered_and_keeps_audit_row(repository, resolver):
    result = persist_new(repository)
    client = RecordingTelegramClient()
    dispatcher = NotificationDispatcher(
        repository,
        route_resolver=resolver,
        telegram_client=client,
        retry_delay_seconds=900,
        batch_limit=10,
        now=lambda: NOW,
    )

    assert dispatcher.dispatch_due() == (1, 0)
    event = repository.get_notification_event(result.notification_event_id)

    assert len(client.calls) == 1
    assert event.attempt_count == 1
    assert event.last_attempt_at == NOW
    assert event.delivered_at == NOW
    assert event.last_error is None
    assert event.next_attempt_at is None
    assert dispatcher.dispatch_due() == (0, 0)


def test_incomplete_repost_uses_publication_event_date_without_degrading_snapshot(repository, resolver):
    complete = vacancy(published_at=date(2026, 11, 1))
    inserted = repository.persist_observation(
        complete,
        observation_complete=True,
        observed_at=NOW,
        notification_intent=NotificationIntent(route="main", profile_slug="python"),
    )
    listing_only_repost = replace(
        complete,
        title="Listing-only title",
        description="Listing-only description",
        published_at=date(2026, 11, 15),
        scraped_at=LATER,
    )
    repost = repository.persist_observation(
        listing_only_repost,
        observation_complete=False,
        observed_at=LATER,
        notification_intent=NotificationIntent(route="main", profile_slug="python"),
    )
    client = RecordingTelegramClient()
    dispatcher = NotificationDispatcher(
        repository,
        route_resolver=resolver,
        telegram_client=client,
        retry_delay_seconds=900,
        batch_limit=10,
        now=lambda: LATER,
    )

    assert repost.status is PersistenceStatus.REPUBLISHED
    assert repost.notification_enqueued is True
    assert repository.get_vacancy_by_id(inserted.vacancy_id) == complete
    assert dispatcher.dispatch_due() == (2, 0)
    assert "🔄 *Повторно опублікована вакансія*" in client.calls[1][1]
    assert "*Опубліковано:* 2026\\-11\\-15" in client.calls[1][1]
    assert "2026\\-11\\-01" not in client.calls[1][1]


def test_dispatch_failure_is_pending_sanitized_and_not_retried_until_due(repository, resolver):
    result = persist_new(repository)
    client = RecordingTelegramClient(error=RuntimeError("token super-secret must not be stored"))
    dispatcher = NotificationDispatcher(
        repository,
        route_resolver=resolver,
        telegram_client=client,
        retry_delay_seconds=900,
        batch_limit=10,
        now=lambda: NOW,
    )

    assert dispatcher.dispatch_due() == (0, 1)
    event = repository.get_notification_event(result.notification_event_id)

    assert event.delivered_at is None
    assert event.attempt_count == 1
    assert event.next_attempt_at == LATER
    assert "super-secret" not in event.last_error
    assert dispatcher.dispatch_due() == (0, 0)
    assert len(client.calls) == 1


def test_due_failed_event_retries_in_a_later_repository_session_without_new_vacancy(tmp_path, resolver):
    db_path = tmp_path / "retry.db"
    first_repository = SQLiteVacancyRepository(db_path)
    first_repository.open()
    result = persist_new(first_repository)
    failed_dispatcher = NotificationDispatcher(
        first_repository,
        route_resolver=resolver,
        telegram_client=RecordingTelegramClient(error=RuntimeError("outage")),
        retry_delay_seconds=900,
        batch_limit=10,
        now=lambda: NOW,
    )
    assert failed_dispatcher.dispatch_due() == (0, 1)
    first_repository.close()

    second_repository = SQLiteVacancyRepository(db_path)
    second_repository.open()
    successful_client = RecordingTelegramClient()
    retry_dispatcher = NotificationDispatcher(
        second_repository,
        route_resolver=resolver,
        telegram_client=successful_client,
        retry_delay_seconds=900,
        batch_limit=10,
        now=lambda: LATER,
    )

    assert retry_dispatcher.dispatch_due() == (1, 0)
    event = second_repository.get_notification_event(result.notification_event_id)
    assert event.attempt_count == 2
    assert event.delivered_at == LATER
    assert len(successful_client.calls) == 1
    assert second_repository.connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 1
    assert second_repository.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
    second_repository.close()


def test_pending_event_formats_the_current_canonical_snapshot_on_retry(repository, resolver):
    result = persist_new(repository)
    failed_dispatcher = NotificationDispatcher(
        repository,
        route_resolver=resolver,
        telegram_client=RecordingTelegramClient(error=RuntimeError("outage")),
        retry_delay_seconds=900,
        batch_limit=10,
        now=lambda: NOW,
    )
    assert failed_dispatcher.dispatch_due() == (0, 1)

    refreshed = replace(vacancy(), title="Refreshed canonical role", scraped_at=NOW + timedelta(minutes=1))
    update = repository.persist_observation(
        refreshed,
        observation_complete=True,
        observed_at=refreshed.scraped_at,
        notification_intent=NotificationIntent(route="alternate", profile_slug="support"),
    )
    client = RecordingTelegramClient()
    retry_dispatcher = NotificationDispatcher(
        repository,
        route_resolver=resolver,
        telegram_client=client,
        retry_delay_seconds=900,
        batch_limit=10,
        now=lambda: LATER,
    )

    assert update.status is PersistenceStatus.UPDATED
    assert retry_dispatcher.dispatch_due() == (1, 0)
    assert "Refreshed canonical role" in client.calls[0][1]
    assert repository.get_notification_event(result.notification_event_id).route == "main"


def test_route_context_preserves_formal_routes_and_ad_hoc_main_default():
    class FormalSpider:
        category = "support"
        notification_route = "alternate"

    class AdHocSpider:
        category = "data_engineer_remote"
        notification_route = None

    assert notification_context_from_spider(FormalSpider()).route == "alternate"
    ad_hoc = notification_context_from_spider(AdHocSpider())
    assert ad_hoc.profile_slug == "data_engineer_remote"
    assert ad_hoc.route == "main"


def test_unknown_or_missing_route_config_fails_without_silent_misdirection():
    resolver = TelegramRouteResolver({"main": TelegramDestination(token="", chat_id="")})

    with pytest.raises(NotificationConfigurationError, match="not configured"):
        resolver.resolve("main")
    with pytest.raises(NotificationConfigurationError, match="Unknown logical notification route"):
        resolver.resolve("alternate")


@pytest.mark.parametrize(
    ("profile_slug", "expected_route"),
    [
        ("python", "main"),
        ("data_engineer", "main"),
        ("other", "alternate"),
        ("support", "alternate"),
    ],
)
def test_formal_profile_routing_values_remain_stable(profile_slug, expected_route):
    assert get_search_profile(profile_slug).notification_route == expected_route


def test_unknown_route_event_stays_pending_with_safe_failure(repository, resolver):
    result = persist_new(repository, route="unknown-route")
    client = RecordingTelegramClient()
    dispatcher = NotificationDispatcher(
        repository,
        route_resolver=resolver,
        telegram_client=client,
        retry_delay_seconds=900,
        batch_limit=10,
        now=lambda: NOW,
    )

    assert dispatcher.dispatch_due() == (0, 1)
    event = repository.get_notification_event(result.notification_event_id)
    assert client.calls == []
    assert event.delivered_at is None
    assert "Unknown logical notification route" in event.last_error


def test_notification_outbox_foreign_key_integrity_and_cascade(repository):
    result = persist_new(repository)
    connection = repository.connection

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO notification_outbox (vacancy_id, event_type, event_key, route, created_at)
            VALUES (999, 'VACANCY_INSERTED', 'vacancy:999:inserted', 'main', ?)
            """,
            (NOW.isoformat(),),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO notification_outbox (
                vacancy_id, publication_id, event_type, event_key, route, created_at
            ) VALUES (?, 999, 'VACANCY_REPUBLISHED', 'publication:999:republished', 'main', ?)
            """,
            (result.vacancy_id, NOW.isoformat()),
        )
    connection.rollback()
    connection.execute("DELETE FROM vacancies WHERE id = ?", (result.vacancy_id,))
    connection.commit()

    assert connection.execute("SELECT COUNT(*) FROM vacancy_publications").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0


def test_canonical_formatter_handles_full_minimal_optional_and_special_characters():
    full = vacancy(title="*Role* <unsafe> & value", description="<p>Long &amp; <b>rich</b> text</p>")
    message = format_vacancy_notification(full)
    minimal = format_vacancy_notification(
        vacancy(
            title="Minimal",
            description=None,
            company=None,
            compensation=None,
            experience_months=None,
            locations=(),
            applicant_locations=(),
            category=None,
            tags=(),
            language_requirements=(),
            published_at=None,
        )
    )

    assert "\\*Role\\* <unsafe\\> & value" in message
    assert r"1200–2000 USD \(MONTH\)" in message
    assert "12 міс\\." in message
    assert "Long & rich text" in message
    assert "[Відкрити вакансію](https://djinni.co/jobs/842583-role)" in message
    assert "None" not in minimal
    assert "Views" not in message
    assert "Responses" not in message
    assert "truncate_description" not in message


def test_republished_formatter_adds_compact_event_context_without_changing_inserted_format():
    old_publication = vacancy(published_at=date(2026, 8, 12))
    inserted = format_vacancy_notification(
        old_publication,
        event_type="VACANCY_INSERTED",
        publication_published_at="2026-08-15",
    )
    republished = format_vacancy_notification(
        old_publication,
        event_type="VACANCY_REPUBLISHED",
        publication_published_at="2026-08-15",
    )

    assert "Повторно опублікована вакансія" not in inserted
    assert "🔄 *Повторно опублікована вакансія*" in republished
    assert "*DJINNI\\.CO*" in republished
    assert "2026\\-08\\-12" in inserted
    assert "2026\\-08\\-15" in republished


@pytest.mark.parametrize(
    ("compensation", "expected"),
    [
        (Compensation(min_amount=2000, currency="USD"), "від 2000 USD"),
        (Compensation(max_amount=3000, currency="USD"), "до 3000 USD"),
        (Compensation(min_amount=1000, max_amount=2000, currency="EUR"), "1000–2000 EUR"),
    ],
)
def test_canonical_compensation_formatting(compensation, expected):
    assert expected in format_vacancy_notification(vacancy(compensation=compensation))


def test_telegram_client_uses_markdown_timeout_and_handles_non_2xx_without_retry_loop():
    calls = []

    class Response:
        status_code = 200

    client = TelegramClient(timeout_seconds=12, post=lambda *args, **kwargs: calls.append((args, kwargs)) or Response())
    client.send(TelegramDestination(token="token", chat_id="chat"), "message")

    assert calls[0][1]["timeout"] == 12
    assert calls[0][1]["data"]["parse_mode"] == PARSE_MODE

    class FailureResponse:
        status_code = 500

    with pytest.raises(TelegramDeliveryError, match="HTTP 500"):
        TelegramClient(timeout_seconds=12, post=lambda *_args, **_kwargs: FailureResponse()).send(
            TelegramDestination(token="token", chat_id="chat"), "message"
        )


def test_telegram_client_sanitizes_request_exception_details():
    def fail(*_args, **_kwargs):
        raise requests.Timeout("token must not appear")

    with pytest.raises(TelegramDeliveryError, match="Timeout") as error:
        TelegramClient(timeout_seconds=12, post=fail).send(
            TelegramDestination(token="token", chat_id="chat"), "message"
        )
    assert "must not appear" not in str(error.value)


def test_notification_modules_do_not_depend_on_legacy_scrapy_item_fields():
    root = Path(__file__).parents[1] / "djinni_scraper" / "notifications"
    source = "\n".join(path.read_text() for path in root.glob("*.py"))

    assert "DjinniScraperItem" not in source
    assert "truncate_description" not in source
    assert "views" not in source
    assert "responses" not in source

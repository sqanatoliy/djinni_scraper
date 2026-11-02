import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from djinni_scraper.domain.vacancy import Vacancy
from djinni_scraper.notifications.dispatcher import NotificationDispatcher
from djinni_scraper.notifications.routing import TelegramDestination, TelegramRouteResolver
from djinni_scraper.notifications.telegram import TelegramDeliveryError, TelegramRateLimitError
from djinni_scraper.persistence.sqlite import (
    PERSISTENCE_STATE_MUTATED_STAT,
    NotificationIntent,
    PersistenceStatus,
    SQLiteVacancyRepository,
)
from djinni_scraper.pipelines import SQLitePipeline


NOW = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)


class RecordingTelegramClient:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = []

    def send(self, destination, message):
        self.calls.append((destination, message))
        if self.error is not None:
            raise self.error


class FakeStats:
    def __init__(self):
        self.values = {}

    def set_value(self, key, value):
        self.values[key] = value


class FakeCrawler:
    def __init__(self):
        self.stats = FakeStats()


class FakeSpider:
    logger = logging.getLogger("test.state_mutation_tracking")
    discovery_profile = "python"
    notification_route = "main"

    def __init__(self):
        self.crawler = FakeCrawler()


def vacancy(**overrides) -> Vacancy:
    values = {
        "source": "djinni",
        "source_vacancy_id": "842583",
        "url": "https://djinni.co/jobs/842583-role",
        "title": "Canonical role",
        "description": "Full canonical description.",
        "published_at": date(2026, 11, 1),
        "scraped_at": NOW,
        "discovery_profiles": ("python",),
    }
    values.update(overrides)
    return Vacancy(**values)


def persist(repository, observed_at=NOW, **overrides):
    observed = vacancy(**overrides)
    return repository.persist_observation(
        observed,
        observation_complete=True,
        observed_at=observed_at,
        notification_intent=NotificationIntent(route="main", profile_slug="python"),
    )


def seeded_database(tmp_path):
    db_path = tmp_path / "vacancies.db"
    repository = SQLiteVacancyRepository(db_path)
    repository.open()
    persist(repository)
    repository.close()
    return db_path


def existing_repository(db_path):
    repository = SQLiteVacancyRepository(db_path)
    repository.open()
    assert repository.state_mutated is False
    return repository


def dispatcher(repository, client):
    return NotificationDispatcher(
        repository,
        route_resolver=TelegramRouteResolver(
            {"main": TelegramDestination(token="token", chat_id="chat")}
        ),
        telegram_client=client,
        retry_delay_seconds=900,
        batch_limit=25,
        send_interval_seconds=1,
        now=lambda: LATER,
        sleep=lambda _seconds: None,
    )


def test_schema_initialization_mutates_fresh_bootstrap_but_not_a_restored_v5_database(tmp_path):
    db_path = tmp_path / "fresh.db"
    repository = SQLiteVacancyRepository(db_path)

    repository.open()
    assert repository.state_mutated is True
    repository.close()

    repository.open()
    assert repository.state_mutated is False


def test_pipeline_reports_schema_initialization_for_a_fresh_zero_result_bootstrap(tmp_path):
    pipeline = SQLitePipeline(str(tmp_path / "fresh-pipeline.db"))
    spider = FakeSpider()

    pipeline.open_spider(spider)

    assert spider.crawler.stats.values[PERSISTENCE_STATE_MUTATED_STAT] is True
    pipeline.close_spider(spider)


@pytest.mark.parametrize(
    "observation",
    (
        lambda repository: persist(
            repository,
            source_vacancy_id="new-id",
            url="https://djinni.co/jobs/new-id-role",
        ),
        lambda repository: persist(repository, observed_at=LATER, title="Changed title"),
        lambda repository: persist(repository, observed_at=LATER, published_at=date(2026, 11, 2)),
    ),
)
def test_insert_update_and_republish_commit_durable_mutations(tmp_path, observation):
    repository = existing_repository(seeded_database(tmp_path))

    result = observation(repository)

    assert result.status in {
        PersistenceStatus.INSERTED,
        PersistenceStatus.UPDATED,
        PersistenceStatus.REPUBLISHED,
    }
    assert repository.state_mutated is True


def test_unchanged_observation_is_a_mutation_when_seen_discovery_and_publication_timestamps_advance(tmp_path):
    repository = existing_repository(seeded_database(tmp_path))

    result = persist(repository, observed_at=LATER)
    vacancy_row = repository.connection.execute(
        "SELECT last_seen_at FROM vacancies WHERE id = ?", (result.vacancy_id,)
    ).fetchone()
    discovery_row = repository.connection.execute(
        "SELECT last_discovered_at FROM vacancy_discoveries WHERE vacancy_id = ?", (result.vacancy_id,)
    ).fetchone()
    publication_row = repository.connection.execute(
        "SELECT last_observed_at FROM vacancy_publications WHERE vacancy_id = ?", (result.vacancy_id,)
    ).fetchone()

    assert result.status is PersistenceStatus.UNCHANGED
    assert repository.state_mutated is True
    assert vacancy_row[0] == LATER.isoformat()
    assert discovery_row[0] == LATER.isoformat()
    assert publication_row[0] == LATER.isoformat()


def test_unchanged_observation_with_identical_timestamps_is_a_true_noop(tmp_path):
    repository = existing_repository(seeded_database(tmp_path))

    result = persist(repository, observed_at=NOW)

    assert result.status is PersistenceStatus.UNCHANGED
    assert repository.state_mutated is False


def test_existing_valid_zero_result_is_a_true_noop(tmp_path):
    repository = existing_repository(seeded_database(tmp_path))

    assert repository.state_mutated is False


@pytest.mark.parametrize(
    "error",
    (None, TelegramDeliveryError("temporary failure"), TelegramRateLimitError(60)),
)
def test_notification_only_delivery_failure_and_rate_limit_are_durable_mutations(tmp_path, error):
    repository = existing_repository(seeded_database(tmp_path))
    client = RecordingTelegramClient(error=error)

    outcome = dispatcher(repository, client).dispatch_due()
    event = repository.get_notification_event(1)

    assert repository.state_mutated is True
    assert event.attempt_count == 1
    if error is None:
        assert outcome.delivered_count == 1
        assert event.delivered_at == LATER
    else:
        assert outcome.failed_count == 1
        assert event.delivered_at is None
        assert event.next_attempt_at is not None


def test_dispatch_without_due_events_does_not_mutate_existing_database(tmp_path):
    repository = existing_repository(seeded_database(tmp_path))
    repository.mark_notification_delivered(1, attempted_at=NOW)
    repository.close()
    repository = existing_repository(repository.db_path)

    outcome = dispatcher(repository, RecordingTelegramClient()).dispatch_due()

    assert outcome.delivered_count == 0
    assert outcome.failed_count == 0
    assert repository.state_mutated is False


def test_rolled_back_observation_does_not_set_mutation_signal(tmp_path, monkeypatch):
    db_path = tmp_path / "rollback.db"
    initial = SQLiteVacancyRepository(db_path)
    initial.open()
    initial.close()
    repository = existing_repository(db_path)

    monkeypatch.setattr(
        repository,
        "_merge_discoveries",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("forced failure")),
    )
    with pytest.raises(sqlite3.OperationalError, match="forced failure"):
        persist(repository)

    assert repository.state_mutated is False
    assert repository.connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 0


def test_mutation_signal_latches_for_the_entire_profile_lifecycle(tmp_path):
    repository = existing_repository(seeded_database(tmp_path))
    persist(repository, observed_at=LATER)

    dispatcher(repository, RecordingTelegramClient()).dispatch_due()

    assert repository.state_mutated is True


def test_pipeline_publishes_shared_stat_for_open_and_close_dispatch_phases(tmp_path, monkeypatch):
    db_path = seeded_database(tmp_path)
    resolver = TelegramRouteResolver(
        {"main": TelegramDestination(token="token", chat_id="chat")}
    )
    client = RecordingTelegramClient()
    monkeypatch.setattr(
        "djinni_scraper.pipelines.TelegramRouteResolver.from_environment",
        classmethod(lambda _cls: resolver),
    )
    monkeypatch.setattr("djinni_scraper.pipelines.TelegramClient", lambda **_kwargs: client)
    pipeline = SQLitePipeline(str(db_path), telegram_send_interval_seconds=1)
    spider = FakeSpider()

    pipeline.open_spider(spider)

    assert client.calls
    assert spider.crawler.stats.values[PERSISTENCE_STATE_MUTATED_STAT] is True
    pipeline.close_spider(spider)


def test_pipeline_publishes_open_dispatch_mutation_even_when_dispatch_raises(tmp_path, monkeypatch):
    pipeline = SQLitePipeline(str(seeded_database(tmp_path)))
    spider = FakeSpider()

    def deliver_then_raise(dispatcher):
        dispatcher.repository.mark_notification_delivered(1, attempted_at=LATER)
        raise RuntimeError("interrupted dispatch")

    monkeypatch.setattr(NotificationDispatcher, "dispatch_due", deliver_then_raise)
    with pytest.raises(RuntimeError, match="interrupted dispatch"):
        pipeline.open_spider(spider)

    assert spider.crawler.stats.values[PERSISTENCE_STATE_MUTATED_STAT] is True
    monkeypatch.setattr(NotificationDispatcher, "dispatch_due", lambda _dispatcher: None)
    pipeline.close_spider(spider)


def test_pipeline_publishes_close_phase_notification_mutation(tmp_path, monkeypatch):
    db_path = tmp_path / "close-phase.db"
    initial = SQLiteVacancyRepository(db_path)
    initial.open()
    initial.close()
    resolver = TelegramRouteResolver(
        {"main": TelegramDestination(token="token", chat_id="chat")}
    )
    client = RecordingTelegramClient()
    monkeypatch.setattr(
        "djinni_scraper.pipelines.TelegramRouteResolver.from_environment",
        classmethod(lambda _cls: resolver),
    )
    monkeypatch.setattr("djinni_scraper.pipelines.TelegramClient", lambda **_kwargs: client)
    pipeline = SQLitePipeline(str(db_path), telegram_send_interval_seconds=1)
    spider = FakeSpider()

    pipeline.open_spider(spider)
    persist(pipeline.repository)

    assert client.calls == []
    pipeline.close_spider(spider)

    assert len(client.calls) == 1
    assert spider.crawler.stats.values[PERSISTENCE_STATE_MUTATED_STAT] is True

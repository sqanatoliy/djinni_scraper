import logging
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from scrapy.exceptions import DropItem

from djinni_scraper.domain.vacancy import Vacancy
from djinni_scraper.items import DjinniScraperItem
from djinni_scraper.notifications.dispatcher import NotificationDispatcher
from djinni_scraper.notifications.routing import TelegramDestination, TelegramRouteResolver
from djinni_scraper.pipelines import SQLitePipeline


class FakeSpider:
    logger = logging.getLogger("test.sqlite_pipeline")
    category = "python"
    notification_route = "main"


class RecordingTelegramClient:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = []

    def send(self, destination, message):
        self.calls.append((destination, message))
        if self.error is not None:
            raise self.error


OBSERVED_AT = datetime(2026, 8, 13, 9, 30, tzinfo=timezone.utc)


def canonical_vacancy(**overrides) -> Vacancy:
    values = {
        "source": "djinni",
        "source_vacancy_id": "842583",
        "url": "https://djinni.co/jobs/842583-role",
        "title": "Canonical title",
        "scraped_at": OBSERVED_AT,
        "discovery_profiles": ("python",),
    }
    values.update(overrides)
    return Vacancy(**values)


def parser_item(vacancy: Vacancy | None = None, *, observation_complete: bool = True) -> DjinniScraperItem:
    values = {
        # These parser transport fields deliberately differ from the canonical Vacancy.
        "title": "Ignored legacy title",
        "salary": "Ignored legacy salary",
        "views": "Ignored legacy views",
        "responses": "Ignored legacy responses",
        "truncate_description": "Ignored legacy description",
        "category": "python",
        "canonical_snapshot_complete": observation_complete,
    }
    if vacancy is not None:
        values["canonical_vacancy"] = vacancy
    return DjinniScraperItem(**values)


@pytest.fixture
def opened_pipeline(tmp_path):
    pipeline = SQLitePipeline(str(tmp_path / "vacancies.db"))
    spider = FakeSpider()
    pipeline.open_spider(spider)
    telegram_client = RecordingTelegramClient()
    pipeline.dispatcher = NotificationDispatcher(
        pipeline.repository,
        route_resolver=TelegramRouteResolver(
            {
                "main": TelegramDestination(token="main-token", chat_id="main-chat"),
                "alternate": TelegramDestination(token="alternate-token", chat_id="alternate-chat"),
            }
        ),
        telegram_client=telegram_client,
        retry_delay_seconds=900,
        batch_limit=25,
        logger=spider.logger,
        now=lambda: OBSERVED_AT,
    )
    yield pipeline, spider, telegram_client
    pipeline.close_spider(spider)


def test_pipeline_enqueues_canonical_insert_and_dispatcher_formats_canonical_vacancy(opened_pipeline):
    pipeline, spider, telegram_client = opened_pipeline
    vacancy = canonical_vacancy(title="Canonical * title", description="Full canonical description")

    assert pipeline.process_item(parser_item(vacancy), spider)["canonical_vacancy"] == vacancy
    event = pipeline.repository.get_notification_event(1)
    pipeline.dispatcher.dispatch_due()

    assert event.route == "main"
    assert event.profile_slug == "python"
    assert len(telegram_client.calls) == 1
    message = telegram_client.calls[0][1]
    assert "Canonical \\* title" in message
    assert "Full canonical description" in message
    assert "Ignored legacy" not in message


def test_updated_unchanged_and_discovery_only_observations_do_not_enqueue_or_send(opened_pipeline):
    pipeline, spider, telegram_client = opened_pipeline
    original = canonical_vacancy()
    updated = replace(original, title="Updated canonical title")
    rediscovered = replace(updated, discovery_profiles=("data_engineer",))

    pipeline.process_item(parser_item(original), spider)
    pipeline.process_item(parser_item(updated), spider)
    pipeline.process_item(parser_item(rediscovered), spider)
    pipeline.dispatcher.dispatch_due()

    assert len(telegram_client.calls) == 1
    assert pipeline.repository.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
    assert pipeline.repository.get_discovery_profiles(1) == ("data_engineer", "python")


def test_telegram_failure_keeps_event_pending_without_rolling_back_vacancy(opened_pipeline):
    pipeline, spider, _telegram_client = opened_pipeline
    failing_client = RecordingTelegramClient(error=RuntimeError("Telegram unavailable"))
    pipeline.dispatcher.telegram_client = failing_client
    vacancy = canonical_vacancy()

    pipeline.process_item(parser_item(vacancy), spider)
    delivered, failed = pipeline.dispatcher.dispatch_due()
    event = pipeline.repository.get_notification_event(1)

    assert (delivered, failed) == (0, 1)
    assert pipeline.repository.get_vacancy(vacancy) == vacancy
    assert event.delivered_at is None
    assert event.attempt_count == 1
    assert event.next_attempt_at is not None


def test_missing_canonical_vacancy_fails_clearly_without_outbox_row(opened_pipeline):
    pipeline, spider, telegram_client = opened_pipeline

    with pytest.raises(DropItem, match="canonical_vacancy"):
        pipeline.process_item(parser_item(), spider)

    assert telegram_client.calls == []
    assert pipeline.repository.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0


def test_persistence_error_does_not_create_or_dispatch_notification(opened_pipeline, monkeypatch):
    pipeline, spider, telegram_client = opened_pipeline
    monkeypatch.setattr(
        pipeline.repository,
        "persist_observation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("database unavailable")),
    )

    with pytest.raises(sqlite3.OperationalError, match="database unavailable"):
        pipeline.process_item(parser_item(canonical_vacancy()), spider)

    assert telegram_client.calls == []
    assert pipeline.repository.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0


def test_pipeline_from_crawler_uses_configured_notification_settings(tmp_path):
    db_path = str(tmp_path / "configured.db")

    class FakeCrawler:
        settings = {
            "VACANCY_DB_PATH": db_path,
            "TELEGRAM_TIMEOUT_SECONDS": 12,
            "TELEGRAM_RETRY_DELAY_SECONDS": 345,
            "NOTIFICATION_OUTBOX_BATCH_SIZE": 7,
        }

    pipeline = SQLitePipeline.from_crawler(FakeCrawler())

    assert pipeline.db_path == db_path
    assert pipeline.telegram_timeout_seconds == 12
    assert pipeline.telegram_retry_delay_seconds == 345
    assert pipeline.notification_outbox_batch_size == 7


def test_pipeline_has_no_legacy_formatter_or_inline_sql_and_workflow_keeps_runtime_state_out_of_git():
    root = Path(__file__).parents[1]
    pipeline_source = (root / "djinni_scraper" / "pipelines.py").read_text()
    workflow = (root / ".github" / "workflows" / "djinni_scraper.yml").read_text()

    assert "persist_observation" in pipeline_source
    assert "NotificationIntent" in pipeline_source
    assert "Telegram(" not in pipeline_source
    assert "_legacy_telegram_data" not in pipeline_source
    assert "INSERT INTO" not in pipeline_source
    assert "DB_PATH: ./runtime/vacancies.db" in workflow
    assert "actions/cache/restore@v4" in workflow
    assert "actions/cache/save@v4" in workflow
    assert "jobs.db" not in workflow
    assert "git add" not in workflow
    assert "git push" not in workflow

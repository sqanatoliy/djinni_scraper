import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from scrapy.exceptions import DropItem

from djinni_scraper.domain.vacancy import Vacancy
from djinni_scraper.items import DjinniScraperItem
from djinni_scraper.persistence.sqlite import SQLiteVacancyRepository
from djinni_scraper.pipelines import SQLitePipeline
from djinni_scraper.utils.telegram_utils import Telegram


class FakeSpider:
    logger = logging.getLogger("test.sqlite_pipeline")


def canonical_vacancy(**overrides) -> Vacancy:
    values = {
        "source": "djinni",
        "source_vacancy_id": "842583",
        "url": "https://djinni.co/jobs/842583-role",
        "title": "Canonical title",
        "scraped_at": datetime(2026, 8, 13, 9, 30, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return Vacancy(**values)


def legacy_item(vacancy: Vacancy | None = None) -> DjinniScraperItem:
    values = {
        "title": "Legacy title",
        "salary": "до $2000",
        "company": "Legacy company",
        "views": "Not specified",
        "responses": "Not specified",
        "pub_date": "14:01 12.08.2026",
        "truncate_description": "Legacy description",
        "tags": ["Python"],
        "url": "https://djinni.co/jobs/842583-role/",
        "category": "python",
    }
    if vacancy is not None:
        values["canonical_vacancy"] = vacancy
    return DjinniScraperItem(**values)


@pytest.fixture
def opened_pipeline(tmp_path):
    pipeline = SQLitePipeline(str(tmp_path / "vacancies.db"))
    spider = FakeSpider()
    pipeline.open_spider(spider)
    yield pipeline, spider
    pipeline.close_spider(spider)


def test_pipeline_persists_canonical_vacancy_not_legacy_sql_fields(opened_pipeline, monkeypatch):
    pipeline, spider = opened_pipeline
    vacancy = canonical_vacancy(title="Canonical persisted title")
    item = legacy_item(vacancy)
    calls = []
    monkeypatch.setattr(Telegram, "send_job_to_telegram", lambda _self, data: calls.append(data))

    assert pipeline.process_item(item, spider) is item
    restored = pipeline.repository.get_vacancy(vacancy)
    assert restored.title == "Canonical persisted title"
    assert calls[0]["title"] == "Legacy title"
    assert calls[0]["tags"] == '["Python"]'


def test_missing_canonical_vacancy_fails_clearly_without_notification(opened_pipeline, monkeypatch):
    pipeline, spider = opened_pipeline
    monkeypatch.setattr(Telegram, "send_job_to_telegram", lambda *_args: pytest.fail("Telegram must not run"))

    with pytest.raises(DropItem, match="canonical_vacancy"):
        pipeline.process_item(legacy_item(), spider)


def test_duplicate_does_not_send_telegram_twice(opened_pipeline, monkeypatch):
    pipeline, spider = opened_pipeline
    item = legacy_item(canonical_vacancy())
    calls = []
    monkeypatch.setattr(Telegram, "send_job_to_telegram", lambda _self, data: calls.append(data))

    pipeline.process_item(item, spider)
    pipeline.process_item(item, spider)
    assert len(calls) == 1


def test_commit_happens_before_telegram_bridge(opened_pipeline, monkeypatch, tmp_path):
    pipeline, spider = opened_pipeline
    vacancy = canonical_vacancy()

    def assert_committed(_self, _data):
        connection = sqlite3.connect(pipeline.db_path)
        try:
            assert connection.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 1
        finally:
            connection.close()

    monkeypatch.setattr(Telegram, "send_job_to_telegram", assert_committed)
    pipeline.process_item(legacy_item(vacancy), spider)


def test_database_error_does_not_invoke_telegram(opened_pipeline, monkeypatch):
    pipeline, spider = opened_pipeline
    monkeypatch.setattr(
        pipeline.repository,
        "insert_vacancy",
        lambda _vacancy: (_ for _ in ()).throw(sqlite3.OperationalError("database unavailable")),
    )
    monkeypatch.setattr(Telegram, "send_job_to_telegram", lambda *_args: pytest.fail("Telegram must not run"))

    with pytest.raises(sqlite3.OperationalError, match="database unavailable"):
        pipeline.process_item(legacy_item(canonical_vacancy()), spider)


def test_telegram_failure_after_commit_does_not_roll_back_persisted_vacancy(opened_pipeline, monkeypatch):
    pipeline, spider = opened_pipeline
    vacancy = canonical_vacancy()
    monkeypatch.setattr(
        Telegram,
        "send_job_to_telegram",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("Telegram unavailable")),
    )

    with pytest.raises(RuntimeError, match="Telegram unavailable"):
        pipeline.process_item(legacy_item(vacancy), spider)
    assert pipeline.repository.get_vacancy(vacancy) == vacancy


def test_pipeline_from_crawler_uses_configured_database_path(tmp_path):
    db_path = str(tmp_path / "configured.db")

    class FakeCrawler:
        settings = {"VACANCY_DB_PATH": db_path}

    assert SQLitePipeline.from_crawler(FakeCrawler()).db_path == db_path


def test_pipeline_has_no_legacy_sql_and_workflow_keeps_runtime_state_out_of_git():
    root = Path(__file__).parents[1]
    pipeline_source = (root / "djinni_scraper" / "pipelines.py").read_text()
    workflow = (root / ".github" / "workflows" / "djinni_scraper.yml").read_text()

    assert "INSERT INTO" not in pipeline_source
    assert "SELECT 1 FROM vacancies WHERE url" not in pipeline_source
    assert "DB_PATH: ./runtime/vacancies.db" in workflow
    assert "actions/cache/restore@v4" in workflow
    assert "actions/cache/save@v4" in workflow
    assert "jobs.db" not in workflow
    assert "git add" not in workflow
    assert "git push" not in workflow

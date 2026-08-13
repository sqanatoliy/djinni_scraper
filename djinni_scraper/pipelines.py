"""Scrapy pipeline bridge from canonical vacancies to persistence and legacy Telegram."""

import json

from itemadapter import ItemAdapter
from scrapy.exceptions import DropItem

from djinni_scraper.domain.vacancy import Vacancy
from djinni_scraper.persistence.sqlite import SQLiteVacancyRepository
from djinni_scraper.utils.telegram_utils import Telegram


class DjinniScraperPipeline:
    def process_item(self, item, spider):
        return item


class SQLitePipeline:
    """Persists canonical Vacancy objects before invoking the temporary Telegram bridge."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.repository: SQLiteVacancyRepository | None = None

    @classmethod
    def from_crawler(cls, crawler):
        return cls(db_path=crawler.settings["VACANCY_DB_PATH"])

    def open_spider(self, spider):
        self.repository = SQLiteVacancyRepository(self.db_path)
        self.repository.open()
        spider.logger.info("Canonical SQLite persistence opened at %s", self.db_path)

    def close_spider(self, spider):
        if self.repository is not None:
            self.repository.close()
            self.repository = None
        spider.logger.info("Canonical SQLite persistence closed")

    def process_item(self, item, spider):
        vacancy = ItemAdapter(item).get("canonical_vacancy")
        if not isinstance(vacancy, Vacancy):
            raise DropItem("Canonical persistence requires item['canonical_vacancy'] to be a Vacancy")

        repository = self._require_repository()
        result = repository.insert_vacancy(vacancy)
        if not result.inserted:
            spider.logger.info("Canonical vacancy already persisted: %s", vacancy.url)
            return item

        # The insert has committed before this legacy formatter/sender bridge runs.
        Telegram(spider).send_job_to_telegram(self._legacy_telegram_data(item))
        spider.logger.info("Canonical vacancy persisted: %s", vacancy.url)
        return item

    @staticmethod
    def _legacy_telegram_data(item) -> dict:
        """Keeps DJ-8-out-of-scope Telegram formatting input unchanged."""
        adapter = ItemAdapter(item)
        return {
            "title": adapter.get("title", "No Title"),
            "salary": adapter.get("salary", "Not specified"),
            "company": adapter.get("company", "Unknown"),
            "views": adapter.get("views", 0),
            "responses": adapter.get("responses", 0),
            "pub_date": adapter.get("pub_date", ""),
            "truncate_description": adapter.get("truncate_description", ""),
            "tags": json.dumps(adapter.get("tags", []), ensure_ascii=False),
            "url": adapter.get("url"),
            "category": adapter.get("category"),
        }

    def _require_repository(self) -> SQLiteVacancyRepository:
        if self.repository is None:
            raise RuntimeError("SQLitePipeline used before open_spider")
        return self.repository

"""Scrapy orchestration from canonical observations to transactional notifications."""

from itemadapter import ItemAdapter
from scrapy.exceptions import DropItem

from djinni_scraper.domain.vacancy import Vacancy
from djinni_scraper.notifications.dispatcher import NotificationDispatcher
from djinni_scraper.notifications.routing import (
    TelegramRouteResolver,
    notification_context_from_spider,
)
from djinni_scraper.notifications.telegram import TelegramClient
from djinni_scraper.persistence.sqlite import (
    PERSISTENCE_STATE_MUTATED_STAT,
    NotificationIntent,
    SQLiteVacancyRepository,
)

class SQLitePipeline:
    """Persists canonical observations and dispatches durable notification outbox events."""

    def __init__(
        self,
        db_path: str,
        *,
        telegram_timeout_seconds: int = 20,
        telegram_retry_delay_seconds: int = 900,
        notification_outbox_batch_size: int = 25,
        telegram_send_interval_seconds: float = 1.0,
    ):
        self.db_path = db_path
        self.telegram_timeout_seconds = telegram_timeout_seconds
        self.telegram_retry_delay_seconds = telegram_retry_delay_seconds
        self.notification_outbox_batch_size = notification_outbox_batch_size
        self.telegram_send_interval_seconds = telegram_send_interval_seconds
        self.repository: SQLiteVacancyRepository | None = None
        self.dispatcher: NotificationDispatcher | None = None

    @classmethod
    def from_crawler(cls, crawler):
        return cls(
            db_path=crawler.settings["VACANCY_DB_PATH"],
            telegram_timeout_seconds=crawler.settings["TELEGRAM_TIMEOUT_SECONDS"],
            telegram_retry_delay_seconds=crawler.settings["TELEGRAM_RETRY_DELAY_SECONDS"],
            notification_outbox_batch_size=crawler.settings["NOTIFICATION_OUTBOX_BATCH_SIZE"],
            telegram_send_interval_seconds=crawler.settings["TELEGRAM_SEND_INTERVAL_SECONDS"],
        )

    def open_spider(self, spider):
        self.repository = SQLiteVacancyRepository(self.db_path)
        self.repository.open()
        self.dispatcher = NotificationDispatcher(
            self.repository,
            route_resolver=TelegramRouteResolver.from_environment(),
            telegram_client=TelegramClient(timeout_seconds=self.telegram_timeout_seconds),
            retry_delay_seconds=self.telegram_retry_delay_seconds,
            batch_limit=self.notification_outbox_batch_size,
            send_interval_seconds=self.telegram_send_interval_seconds,
            logger=spider.logger,
        )
        spider.logger.info("Canonical SQLite persistence opened at %s", self.db_path)
        self._publish_mutation_stat(spider)
        try:
            self.dispatcher.dispatch_due()
        finally:
            self._publish_mutation_stat(spider)

    def close_spider(self, spider):
        try:
            if self.dispatcher is not None:
                self.dispatcher.dispatch_due()
        finally:
            self.dispatcher = None
            try:
                self._publish_mutation_stat(spider)
            finally:
                if self.repository is not None:
                    self.repository.close()
                    self.repository = None
            spider.logger.info("Canonical SQLite persistence closed")

    def process_item(self, item, spider):
        adapter = ItemAdapter(item)
        vacancy = adapter.get("canonical_vacancy")
        if not isinstance(vacancy, Vacancy):
            raise DropItem("Canonical persistence requires item['canonical_vacancy'] to be a Vacancy")

        repository = self._require_repository()
        notification_context = notification_context_from_spider(spider)
        result = repository.persist_observation(
            vacancy,
            discovery_profiles=vacancy.discovery_profiles,
            observation_complete=adapter.get("canonical_snapshot_complete") is True,
            observed_at=vacancy.scraped_at,
            notification_intent=NotificationIntent(
                route=notification_context.route,
                profile_slug=notification_context.profile_slug,
            ),
        )
        self._publish_mutation_stat(spider)
        spider.logger.info(
            "canonical-vacancy-persisted url=%s status=%s discovery_added=%s",
            vacancy.url,
            result.status.value,
            result.discovery_added,
        )
        if result.notification_enqueued:
            spider.logger.info(
                "notification-enqueued event_id=%s route=%s profile_slug=%s",
                result.notification_event_id,
                notification_context.route,
                notification_context.profile_slug,
            )
        return item

    def _publish_mutation_stat(self, spider) -> None:
        """Expose committed SQLite mutation state once for all source families."""
        if self.repository is None or not self.repository.state_mutated:
            return
        crawler = getattr(spider, "crawler", None)
        stats = getattr(crawler, "stats", None)
        if stats is not None:
            stats.set_value(PERSISTENCE_STATE_MUTATED_STAT, True)

    def _require_repository(self) -> SQLiteVacancyRepository:
        if self.repository is None:
            raise RuntimeError("SQLitePipeline used before open_spider")
        return self.repository

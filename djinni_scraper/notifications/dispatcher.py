"""Dispatches due canonical notification outbox events without blocking retries."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from djinni_scraper.notifications.routing import TelegramRouteResolver
from djinni_scraper.notifications.routing import NotificationConfigurationError
from djinni_scraper.notifications.telegram import (
    TelegramClient,
    TelegramDeliveryError,
    format_vacancy_notification,
)
from djinni_scraper.persistence.sqlite import SQLiteVacancyRepository


class NotificationDispatcher:
    """Attempts each due event once and persists delivery state for later runs."""

    def __init__(
        self,
        repository: SQLiteVacancyRepository,
        *,
        route_resolver: TelegramRouteResolver,
        telegram_client: TelegramClient,
        retry_delay_seconds: int,
        batch_limit: int,
        logger: logging.Logger | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(retry_delay_seconds, int) or retry_delay_seconds <= 0:
            raise ValueError("notification retry delay must be a positive integer")
        if not isinstance(batch_limit, int) or batch_limit <= 0:
            raise ValueError("notification batch limit must be a positive integer")
        self.repository = repository
        self.route_resolver = route_resolver
        self.telegram_client = telegram_client
        self.retry_delay_seconds = retry_delay_seconds
        self.batch_limit = batch_limit
        self.logger = logger or logging.getLogger(__name__)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def dispatch_due(self) -> tuple[int, int]:
        """Returns ``(delivered, failed)`` after one attempt per currently due event."""
        now = _utc_now(self._now())
        delivered = 0
        failed = 0
        for event in self.repository.list_due_notification_events(now=now, limit=self.batch_limit):
            try:
                vacancy = self.repository.get_vacancy_by_id(event.vacancy_id)
                if vacancy is None:
                    raise RuntimeError("Canonical vacancy for notification event is unavailable")
                publication_published_at = None
                if event.event_type == "VACANCY_REPUBLISHED":
                    if event.publication_id is None:
                        raise RuntimeError("Republished notification event has no publication identity")
                    publication = self.repository.get_publication(event.publication_id)
                    if publication is None or publication.vacancy_id != event.vacancy_id:
                        raise RuntimeError("Publication for republished notification event is unavailable")
                    publication_published_at = publication.published_at
                destination = self.route_resolver.resolve(event.route)
                self.telegram_client.send(
                    destination,
                    format_vacancy_notification(
                        vacancy,
                        event_type=event.event_type,
                        publication_published_at=publication_published_at,
                    ),
                )
                self.repository.mark_notification_delivered(event.id, attempted_at=now)
                self.logger.info("notification-delivered event_id=%s route=%s", event.id, event.route)
                delivered += 1
            except Exception as error:
                retry_at = now + timedelta(seconds=self.retry_delay_seconds)
                self.repository.mark_notification_failed(
                    event.id,
                    attempted_at=now,
                    next_attempt_at=retry_at,
                    error=_safe_dispatch_error(error),
                )
                self.logger.warning(
                    "notification-failed event_id=%s route=%s error=%s",
                    event.id,
                    event.route,
                    _safe_dispatch_error(error),
                )
                failed += 1
        return delivered, failed


def _utc_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("dispatcher clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _safe_dispatch_error(error: Exception) -> str:
    if isinstance(error, (NotificationConfigurationError, TelegramDeliveryError)):
        return " ".join(str(error).split())[:500] or type(error).__name__
    return f"Notification delivery failed: {type(error).__name__}"

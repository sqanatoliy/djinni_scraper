"""Dispatches due canonical notification outbox events without blocking retries."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from djinni_scraper.notifications.routing import TelegramRouteResolver
from djinni_scraper.notifications.routing import NotificationConfigurationError
from djinni_scraper.notifications.telegram import (
    TelegramClient,
    TelegramDeliveryError,
    TelegramRateLimitError,
    format_vacancy_notification,
)
from djinni_scraper.persistence.sqlite import SQLiteVacancyRepository


@dataclass(frozen=True, slots=True)
class NotificationDispatchResult:
    """Durable delivery work completed during one dispatch cycle."""

    delivered_count: int
    failed_count: int
    rate_limited: bool


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
        send_interval_seconds: float = 1.0,
        logger: logging.Logger | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(retry_delay_seconds, int) or retry_delay_seconds <= 0:
            raise ValueError("notification retry delay must be a positive integer")
        if not isinstance(batch_limit, int) or batch_limit <= 0:
            raise ValueError("notification batch limit must be a positive integer")
        if (
            not isinstance(send_interval_seconds, (int, float))
            or isinstance(send_interval_seconds, bool)
            or not math.isfinite(send_interval_seconds)
            or send_interval_seconds <= 0
        ):
            raise ValueError("notification send interval must be a positive number")
        self.repository = repository
        self.route_resolver = route_resolver
        self.telegram_client = telegram_client
        self.retry_delay_seconds = retry_delay_seconds
        self.batch_limit = batch_limit
        self.send_interval_seconds = float(send_interval_seconds)
        self.logger = logger or logging.getLogger(__name__)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._blocked_until_by_route: dict[str, datetime] = {}

    def dispatch_due(self) -> NotificationDispatchResult:
        """Dispatches one ordered batch and stops safely at a Telegram rate limit."""
        delivered = 0
        failed = 0
        events = self.repository.list_due_notification_events(
            now=_utc_now(self._now()), limit=self.batch_limit
        )
        for index, event in enumerate(events):
            now = _utc_now(self._now())
            blocked_until = self._blocked_until_by_route.get(event.route)
            if blocked_until is not None and now < blocked_until:
                self.logger.info(
                    "notification-dispatch-paused route=%s blocked_until=%s",
                    event.route,
                    blocked_until.isoformat(),
                )
                return NotificationDispatchResult(delivered, failed, rate_limited=True)
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
                next_event = events[index + 1] if index < len(events) - 1 else None
                next_blocked_until = (
                    self._blocked_until_by_route.get(next_event.route) if next_event is not None else None
                )
                if next_event is not None and (next_blocked_until is None or now >= next_blocked_until):
                    self._sleep(self.send_interval_seconds)
            except TelegramRateLimitError as error:
                retry_after_seconds = error.retry_after_seconds or self.retry_delay_seconds
                retry_at = now + timedelta(seconds=retry_after_seconds)
                self.repository.mark_notification_failed(
                    event.id,
                    attempted_at=now,
                    next_attempt_at=retry_at,
                    error=_safe_dispatch_error(error),
                )
                self._blocked_until_by_route[event.route] = retry_at
                self.logger.warning(
                    "notification-rate-limited event_id=%s route=%s retry_after=%ss blocked_until=%s",
                    event.id,
                    event.route,
                    retry_after_seconds,
                    retry_at.isoformat(),
                )
                return NotificationDispatchResult(delivered, failed + 1, rate_limited=True)
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
        return NotificationDispatchResult(delivered, failed, rate_limited=False)


def _utc_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("dispatcher clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _safe_dispatch_error(error: Exception) -> str:
    if isinstance(error, (NotificationConfigurationError, TelegramDeliveryError)):
        return " ".join(str(error).split())[:500] or type(error).__name__
    return f"Notification delivery failed: {type(error).__name__}"

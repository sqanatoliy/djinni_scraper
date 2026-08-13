"""Logical notification routing independent from search filters and secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass


MAIN_ROUTE = "main"
ALTERNATE_ROUTE = "alternate"
DEFAULT_AD_HOC_ROUTE = MAIN_ROUTE


class NotificationConfigurationError(RuntimeError):
    """A stored logical route cannot be resolved to a safe Telegram destination."""


@dataclass(frozen=True, slots=True)
class NotificationContext:
    """Observation metadata passed from spider to persistence, never to Vacancy."""

    profile_slug: str
    route: str

    def __post_init__(self) -> None:
        for field_name in ("profile_slug", "route"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"notification {field_name} must be a non-empty string")
            object.__setattr__(self, field_name, value.strip())


@dataclass(frozen=True, slots=True)
class TelegramDestination:
    """Resolved destination held only in memory, never persisted or logged."""

    token: str
    chat_id: str


class TelegramRouteResolver:
    """Maps stored logical routes to the existing environment credential contract."""

    def __init__(self, destinations: dict[str, TelegramDestination]):
        self._destinations = dict(destinations)

    @classmethod
    def from_environment(cls) -> "TelegramRouteResolver":
        """Uses existing main/alternate secret names without putting secrets in profiles."""
        return cls(
            {
                MAIN_ROUTE: TelegramDestination(
                    token=os.environ.get("TELEGRAM_TOKEN", "").strip(),
                    chat_id=os.environ.get("CHAT_ID", "").strip(),
                ),
                ALTERNATE_ROUTE: TelegramDestination(
                    token=os.environ.get("NO_EXP_TELEGRAM_TOKEN", "").strip(),
                    chat_id=os.environ.get("NO_EXP_CHAT_ID", "").strip(),
                ),
            }
        )

    def resolve(self, route: str) -> TelegramDestination:
        destination = self._destinations.get(route)
        if destination is None:
            raise NotificationConfigurationError(f"Unknown logical notification route: {route!r}")
        if not destination.token or not destination.chat_id:
            raise NotificationConfigurationError(
                f"Telegram destination for logical route {route!r} is not configured"
            )
        return destination


def notification_context_from_spider(spider) -> NotificationContext:
    """Preserves formal profile routes and gives ad-hoc crawls the historical main default."""
    profile_slug = getattr(spider, "discovery_profile", None)
    if profile_slug is None:
        profile_slug = getattr(spider, "category", None)
    if not isinstance(profile_slug, str) or not profile_slug.strip():
        profile_slug = "all"
    route = getattr(spider, "notification_route", None)
    if not isinstance(route, str) or not route.strip():
        route = DEFAULT_AD_HOC_ROUTE
    return NotificationContext(profile_slug=profile_slug, route=route)

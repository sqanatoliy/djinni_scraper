"""Canonical notification formatting, routing, transport, and outbox dispatch."""

from .dispatcher import NotificationDispatcher
from .routing import NotificationContext, TelegramRouteResolver
from .telegram import TelegramClient, format_vacancy_notification

__all__ = [
    "NotificationContext",
    "NotificationDispatcher",
    "TelegramClient",
    "TelegramRouteResolver",
    "format_vacancy_notification",
]

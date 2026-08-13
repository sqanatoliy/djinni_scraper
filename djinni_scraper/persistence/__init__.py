"""Persistence adapters for canonical domain objects."""

from .sqlite import (
    NotificationIntent,
    NotificationOutboxEvent,
    PersistenceResult,
    PersistenceStatus,
    SQLiteVacancyRepository,
    VacancyDiscovery,
)

__all__ = [
    "NotificationIntent",
    "NotificationOutboxEvent",
    "PersistenceResult",
    "PersistenceStatus",
    "SQLiteVacancyRepository",
    "VacancyDiscovery",
]

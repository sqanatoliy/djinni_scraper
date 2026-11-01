"""Persistence adapters for canonical domain objects."""

from .sqlite import (
    NotificationIntent,
    NotificationOutboxEvent,
    PersistenceResult,
    PersistenceStatus,
    SQLiteVacancyRepository,
    VacancyDiscovery,
    VacancyPublication,
    VacancyVersion,
)

__all__ = [
    "NotificationIntent",
    "NotificationOutboxEvent",
    "PersistenceResult",
    "PersistenceStatus",
    "SQLiteVacancyRepository",
    "VacancyDiscovery",
    "VacancyPublication",
    "VacancyVersion",
]

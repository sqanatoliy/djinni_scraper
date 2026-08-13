"""Persistence adapters for canonical domain objects."""

from .sqlite import PersistenceResult, PersistenceStatus, SQLiteVacancyRepository, VacancyDiscovery

__all__ = ["PersistenceResult", "PersistenceStatus", "SQLiteVacancyRepository", "VacancyDiscovery"]

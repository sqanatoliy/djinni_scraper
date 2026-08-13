"""Persistence adapters for canonical domain objects."""

from .sqlite import InsertResult, SQLiteVacancyRepository

__all__ = ["InsertResult", "SQLiteVacancyRepository"]

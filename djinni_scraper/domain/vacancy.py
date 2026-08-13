"""Canonical, source-independent vacancy domain model."""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any


CanonicalDate = date | datetime


@dataclass(frozen=True, slots=True)
class Company:
    """A minimal employer representation shared by vacancy sources."""

    name: str | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class Compensation:
    """Source-provided compensation values without currency conversion or inference."""

    min_amount: int | float | None = None
    max_amount: int | float | None = None
    currency: str | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class Location:
    """One vacancy or applicant-location requirement."""

    country: str | None = None
    city: str | None = None
    region: str | None = None


@dataclass(frozen=True, slots=True)
class LanguageRequirement:
    """A language requirement as supplied by a source."""

    language: str
    level: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.language, str) or not self.language.strip():
            raise ValueError("language requirement must include a language")


@dataclass(frozen=True, slots=True)
class Vacancy:
    """Normalized vacancy content and provenance, independent from scraper implementations."""

    source: str
    url: str
    title: str
    scraped_at: datetime
    source_vacancy_id: str | None = None
    description: str | None = None
    company: Company | None = None
    compensation: Compensation | None = None
    employment_type: str | None = None
    experience_months: int | float | None = None
    locations: tuple[Location, ...] = ()
    applicant_locations: tuple[Location, ...] = ()
    category: str | None = None
    industry: str | None = None
    tags: tuple[str, ...] = ()
    language_requirements: tuple[LanguageRequirement, ...] = ()
    published_at: CanonicalDate | None = None
    valid_through: CanonicalDate | None = None
    discovery_profiles: tuple[str, ...] = ()
    raw_source_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("source", "url", "title"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Vacancy {name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())

        if self.source_vacancy_id is not None:
            if not isinstance(self.source_vacancy_id, str) or not self.source_vacancy_id.strip():
                raise ValueError("Vacancy source_vacancy_id must be a non-empty string or None")
            object.__setattr__(self, "source_vacancy_id", self.source_vacancy_id.strip())

        if not isinstance(self.scraped_at, datetime) or self.scraped_at.tzinfo is None:
            raise ValueError("Vacancy scraped_at must be a timezone-aware datetime")
        if self.scraped_at.utcoffset() is None:
            raise ValueError("Vacancy scraped_at must include a UTC offset")

        for name in ("published_at", "valid_through"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, (date, datetime)):
                raise ValueError(f"Vacancy {name} must be a date, datetime, or None")

        for name in ("locations", "applicant_locations", "tags", "language_requirements", "discovery_profiles"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "raw_source_metadata", _freeze_json_value(self.raw_source_metadata))

    def to_dict(self) -> dict[str, Any]:
        """Returns a deterministic JSON-compatible representation."""
        return {field_.name: _to_json_compatible(getattr(self, field_.name)) for field_ in fields(self)}


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json_value(value[key]) for key in sorted(value, key=str)}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("raw_source_metadata must contain JSON-compatible values")


def _to_json_compatible(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value):
        return {field_.name: _to_json_compatible(getattr(value, field_.name)) for field_ in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _to_json_compatible(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, tuple):
        return [_to_json_compatible(item) for item in value]
    return value

"""Typed, source-specific DOU vacancy discovery profiles and URL construction."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping
from urllib.parse import quote_plus


DOU_SOURCE = "dou"
DOU_VACANCIES_URL = "https://jobs.dou.ua/vacancies/"
DOU_FIRST_JOB_URL = "https://jobs.dou.ua/first-job/"


@dataclass(frozen=True, slots=True)
class DouSearchQuery:
    """Validated DOU filters without source transport or notification concerns."""

    category: str | None = None
    experience: str | None = None
    city: str | None = None
    remote: bool = False
    relocation: bool = False
    first_job: bool = False

    def __post_init__(self) -> None:
        for name in ("category", "experience", "city"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"DOU search {name} must be a non-empty string or None")
                object.__setattr__(self, name, value.strip())
        for name in ("remote", "relocation", "first_job"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"DOU search {name} must be a boolean")

        location_filters = sum(bool(value) for value in (self.remote, self.relocation, self.city))
        if location_filters > 1:
            raise ValueError("Only one DOU location filter of remote, relocation, or city may be set")
        if self.first_job:
            if self.category is not None or self.experience is not None:
                raise ValueError("DOU first-job search must not include category or experience")
            if self.remote or self.relocation:
                raise ValueError("DOU first-job search uses city rather than remote or relocation flags")
            if self.city is None:
                raise ValueError("DOU first-job search requires a city filter")

    @property
    def path(self) -> str:
        return "/first-job/" if self.first_job else "/vacancies/"

    def query_parts(self) -> tuple[str, ...]:
        """Builds ordered query parts, retaining DOU's bare location flags."""
        parts: list[str] = []
        if self.remote:
            parts.append("remote")
        elif self.relocation:
            parts.append("relocation")
        elif self.city is not None:
            parts.append(f"city={quote_plus(self.city)}")
        if not self.first_job:
            if self.category is not None:
                parts.append(f"category={quote_plus(self.category)}")
            if self.experience is not None:
                parts.append(f"exp={quote_plus(self.experience)}")
        return tuple(parts)

    def build_url(self, *, xhr: bool = False) -> str:
        """Returns a deterministic DOU listing or XHR URL with unchanged filters."""
        if xhr and self.first_job:
            raise ValueError("DOU first-job search does not use the vacancies XHR endpoint")
        base = DOU_VACANCIES_URL if not self.first_job else DOU_FIRST_JOB_URL
        if xhr:
            base = f"{DOU_VACANCIES_URL}xhr-load/"
        query = "&".join(self.query_parts())
        return f"{base}?{query}" if query else base


@dataclass(frozen=True, slots=True)
class DouSearchProfile:
    """One formal DOU discovery profile with its existing logical notification route."""

    slug: str
    source: Literal["dou"]
    notification_route: str
    query: DouSearchQuery

    def __post_init__(self) -> None:
        for name in ("slug", "source", "notification_route"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"DOU SearchProfile {name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if self.source != DOU_SOURCE:
            raise ValueError(f"DOU SearchProfile source must be {DOU_SOURCE!r}")
        if not isinstance(self.query, DouSearchQuery):
            raise ValueError("DOU SearchProfile query must be a DouSearchQuery")

    @property
    def search_url(self) -> str:
        return self.query.build_url()

    @property
    def xhr_url(self) -> str | None:
        return None if self.query.first_job else self.query.build_url(xhr=True)


def _build_registry(profiles: tuple[DouSearchProfile, ...]) -> Mapping[str, DouSearchProfile]:
    registry: dict[str, DouSearchProfile] = {}
    for profile in profiles:
        if profile.slug in registry:
            raise ValueError(f"Duplicate DOU search profile slug: {profile.slug}")
        registry[profile.slug] = profile
    return MappingProxyType(registry)


DOU_SEARCH_PROFILES = _build_registry(
    (
        DouSearchProfile(
            slug="dou-python-0-1",
            source=DOU_SOURCE,
            notification_route="main",
            query=DouSearchQuery(category="Python", experience="0-1"),
        ),
        DouSearchProfile(
            slug="dou-python-1-3",
            source=DOU_SOURCE,
            notification_route="main",
            query=DouSearchQuery(category="Python", experience="1-3"),
        ),
        DouSearchProfile(
            slug="dou-data-engineer-0-1",
            source=DOU_SOURCE,
            notification_route="main",
            query=DouSearchQuery(category="Data Engineer", experience="0-1"),
        ),
        DouSearchProfile(
            slug="dou-data-engineer-1-3",
            source=DOU_SOURCE,
            notification_route="main",
            query=DouSearchQuery(category="Data Engineer", experience="1-3"),
        ),
        DouSearchProfile(
            slug="dou-first-job-remote",
            source=DOU_SOURCE,
            notification_route="alternate",
            query=DouSearchQuery(city="remote", first_job=True),
        ),
        DouSearchProfile(
            slug="dou-support-remote-0-1",
            source=DOU_SOURCE,
            notification_route="alternate",
            query=DouSearchQuery(category="Support", experience="0-1", remote=True),
        ),
    )
)


def list_dou_search_profiles() -> tuple[DouSearchProfile, ...]:
    """Lists the deliberately ordered formal DOU profiles."""
    return tuple(DOU_SEARCH_PROFILES.values())


def get_dou_search_profile(slug: str) -> DouSearchProfile:
    """Returns one configured DOU profile or raises an actionable error."""
    normalized_slug = slug.strip() if isinstance(slug, str) else ""
    profile = DOU_SEARCH_PROFILES.get(normalized_slug)
    if profile is None:
        available = ", ".join(DOU_SEARCH_PROFILES)
        raise ValueError(f"Unknown DOU search profile: {slug!r}. Available profiles: {available}")
    return profile

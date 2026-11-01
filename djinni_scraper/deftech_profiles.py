"""Typed DefTech DOU discovery profiles and deterministic URL construction."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping
from urllib.parse import quote_plus


DOU_SOURCE = "dou"
DEFTECH_JOBS_URL = "https://deftech.dou.ua/jobs/"


@dataclass(frozen=True, slots=True)
class DefTechSearchQuery:
    """Validated public DefTech filters, limited to parameters observed on its UI."""

    category: str | None = None
    experience: str | None = None
    city: str | None = None
    remote: bool = False
    search: str | None = None
    search_in_description: bool = False

    def __post_init__(self) -> None:
        for name in ("category", "experience", "city", "search"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"DefTech search {name} must be a non-empty string or None")
                object.__setattr__(self, name, value.strip())
        for name in ("remote", "search_in_description"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"DefTech search {name} must be a boolean")

    @property
    def first_job(self) -> bool:
        """Satisfies the shared DOU listing transport without adding a DefTech filter."""
        return False

    def query_parts(self) -> tuple[str, ...]:
        """Builds deterministic DefTech query parts, including its bare remote flag."""
        parts: list[str] = []
        if self.remote:
            parts.append("remote")
        if self.city is not None:
            parts.append(f"city={quote_plus(self.city)}")
        if self.category is not None:
            parts.append(f"category={quote_plus(self.category)}")
        if self.experience is not None:
            parts.append(f"exp={quote_plus(self.experience)}")
        if self.search is not None:
            parts.append(f"search={quote_plus(self.search)}")
        if self.search_in_description:
            parts.append("descr=1")
        return tuple(parts)

    def build_url(self, *, xhr: bool = False) -> str:
        """Returns a deterministic DefTech listing or XHR URL with unchanged filters."""
        base = f"{DEFTECH_JOBS_URL}xhr-load/" if xhr else DEFTECH_JOBS_URL
        query = "&".join(self.query_parts())
        return f"{base}?{query}" if query else base


@dataclass(frozen=True, slots=True)
class DefTechSearchProfile:
    """One formal DefTech search surface profile with DOU canonical identity."""

    slug: str
    source: Literal["dou"]
    notification_route: str
    query: DefTechSearchQuery

    def __post_init__(self) -> None:
        for name in ("slug", "source", "notification_route"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"DefTech SearchProfile {name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if self.source != DOU_SOURCE:
            raise ValueError(f"DefTech SearchProfile source must be {DOU_SOURCE!r}")
        if not isinstance(self.query, DefTechSearchQuery):
            raise ValueError("DefTech SearchProfile query must be a DefTechSearchQuery")

    @property
    def search_url(self) -> str:
        return self.query.build_url()

    @property
    def xhr_url(self) -> str:
        return self.query.build_url(xhr=True)


def _build_registry(profiles: tuple[DefTechSearchProfile, ...]) -> Mapping[str, DefTechSearchProfile]:
    registry: dict[str, DefTechSearchProfile] = {}
    for profile in profiles:
        if profile.slug in registry:
            raise ValueError(f"Duplicate DefTech search profile slug: {profile.slug}")
        registry[profile.slug] = profile
    return MappingProxyType(registry)


DEFTECH_SEARCH_PROFILES = _build_registry(
    (
        DefTechSearchProfile(
            slug="deftech-data-engineer",
            source=DOU_SOURCE,
            notification_route="main",
            query=DefTechSearchQuery(category="Data Engineer"),
        ),
    )
)


def list_deftech_search_profiles() -> tuple[DefTechSearchProfile, ...]:
    """Lists deliberately ordered production DefTech profiles."""
    return tuple(DEFTECH_SEARCH_PROFILES.values())


def get_deftech_search_profile(slug: str) -> DefTechSearchProfile:
    """Returns one configured DefTech profile or raises an actionable error."""
    normalized_slug = slug.strip() if isinstance(slug, str) else ""
    profile = DEFTECH_SEARCH_PROFILES.get(normalized_slug)
    if profile is None:
        available = ", ".join(DEFTECH_SEARCH_PROFILES)
        raise ValueError(f"Unknown DefTech search profile: {slug!r}. Available profiles: {available}")
    return profile

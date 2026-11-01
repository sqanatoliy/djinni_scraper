"""Immutable application configuration for Djinni vacancy discovery profiles."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType


DJINNI_SOURCE = "djinni"
COMMON_ENGLISH_LEVELS = ("no_english", "basic", "pre", "intermediate")


@dataclass(frozen=True, slots=True)
class DjinniSearchQuery:
    """The stable Djinni jobs query fields currently used by formal profiles."""

    primary_keyword: str
    exp_level: tuple[str, ...]
    english_level: tuple[str, ...]
    employment: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.primary_keyword, str) or not self.primary_keyword.strip():
            raise ValueError("Djinni search primary_keyword must be a non-empty string")
        object.__setattr__(self, "primary_keyword", self.primary_keyword.strip())
        object.__setattr__(self, "exp_level", _required_values(self.exp_level, "exp_level"))
        object.__setattr__(self, "english_level", _required_values(self.english_level, "english_level"))
        object.__setattr__(self, "employment", _optional_values(self.employment, "employment"))

    def to_params(self) -> Mapping[str, tuple[str, ...]]:
        """Returns immutable URL-builder input without performing URL encoding."""
        params = {
            "search_type": ("basic-search",),
            "primary_keyword": (self.primary_keyword,),
            "exp_level": self.exp_level,
            "english_level": self.english_level,
        }
        if self.employment:
            params["employment"] = self.employment
        return MappingProxyType(params)


@dataclass(frozen=True, slots=True)
class SearchProfile:
    """One logical vacancy-discovery configuration, with no secrets attached."""

    slug: str
    source: str
    query: DjinniSearchQuery
    notification_route: str

    def __post_init__(self) -> None:
        for name in ("slug", "source", "notification_route"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"SearchProfile {name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if self.source != DJINNI_SOURCE:
            raise ValueError(f"Djinni SearchProfile source must be {DJINNI_SOURCE!r}")
        if not isinstance(self.query, DjinniSearchQuery):
            raise ValueError("SearchProfile query must be a DjinniSearchQuery")

    @property
    def query_params(self) -> Mapping[str, tuple[str, ...]]:
        """Exposes query data for URL construction without leaking mutable collections."""
        return self.query.to_params()


def _required_values(values: Iterable[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    try:
        normalized = tuple(value.strip() for value in values if isinstance(value, str) and value.strip())
    except TypeError as error:
        raise ValueError(f"Djinni search {field_name} must contain non-empty strings") from error
    if not normalized:
        raise ValueError(f"Djinni search {field_name} must contain at least one value")
    return normalized


def _optional_values(values: Iterable[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    try:
        normalized = tuple(value.strip() for value in values if isinstance(value, str) and value.strip())
    except TypeError as error:
        raise ValueError(f"Djinni search {field_name} must contain strings") from error
    return normalized


def _build_registry(profiles: Iterable[SearchProfile]) -> Mapping[str, SearchProfile]:
    registry = {}
    for profile in profiles:
        if profile.slug in registry:
            raise ValueError(f"Duplicate Djinni search profile slug: {profile.slug}")
        registry[profile.slug] = profile
    return MappingProxyType(registry)


SEARCH_PROFILES = _build_registry(
    (
        SearchProfile(
            slug="python",
            source=DJINNI_SOURCE,
            query=DjinniSearchQuery(
                primary_keyword="Python",
                exp_level=("no_exp", "1y", "2y"),
                english_level=COMMON_ENGLISH_LEVELS,
            ),
            notification_route="main",
        ),
        SearchProfile(
            slug="data_engineer",
            source=DJINNI_SOURCE,
            query=DjinniSearchQuery(
                primary_keyword="Data Engineer",
                exp_level=("no_exp", "1y"),
                english_level=COMMON_ENGLISH_LEVELS,
            ),
            notification_route="main",
        ),
        SearchProfile(
            slug="other",
            source=DJINNI_SOURCE,
            query=DjinniSearchQuery(
                primary_keyword="Other",
                exp_level=("no_exp", "1y"),
                english_level=COMMON_ENGLISH_LEVELS,
            ),
            notification_route="alternate",
        ),
        SearchProfile(
            slug="support",
            source=DJINNI_SOURCE,
            query=DjinniSearchQuery(
                primary_keyword="Support",
                exp_level=("no_exp", "1y"),
                english_level=COMMON_ENGLISH_LEVELS,
            ),
            notification_route="alternate",
        ),
    )
)


def list_search_profiles() -> tuple[SearchProfile, ...]:
    """Lists profiles in their deliberate registry order."""
    return tuple(SEARCH_PROFILES.values())


def get_search_profile(slug: str) -> SearchProfile:
    """Returns a known profile or raises an actionable configuration error."""
    normalized_slug = slug.strip() if isinstance(slug, str) else ""
    profile = SEARCH_PROFILES.get(normalized_slug)
    if profile is None:
        available = ", ".join(SEARCH_PROFILES)
        raise ValueError(f"Unknown Djinni search profile: {slug!r}. Available profiles: {available}")
    return profile

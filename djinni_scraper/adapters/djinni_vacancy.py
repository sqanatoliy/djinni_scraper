"""Normalization from Djinni parsed item fields to the canonical Vacancy model."""

import logging
import math
from collections.abc import Mapping
from datetime import date, datetime, timezone
from itertools import zip_longest
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from djinni_scraper.domain.vacancy import Company, Compensation, LanguageRequirement, Location, Vacancy


LOGGER = logging.getLogger(__name__)
SOURCE = "djinni"
LEGACY_MISSING_VALUES = frozenset({"not specified"})


def normalize_djinni_vacancy(
    item: Mapping[str, Any],
    *,
    scraped_at: datetime | None = None,
    logger: logging.Logger | None = None,
) -> Vacancy:
    """Builds a canonical Vacancy while retaining Djinni-only evidence as raw metadata."""
    logger = logger or LOGGER
    url = _normalize_url(_first_text(item, "canonical_url", "url"))
    title = _first_text(item, "detail_title", "title")

    published_at = _parse_source_date(_value(item, "date_posted"), "date_posted", logger)
    if published_at is None:
        published_at = _parse_listing_date(_value(item, "pub_date"), logger)

    company_name = _first_text(item, "company_name", "company")
    company_url = _optional_text(_value(item, "company_url"))
    company = Company(name=company_name, url=company_url) if company_name or company_url else None

    return Vacancy(
        source=SOURCE,
        source_vacancy_id=_optional_text(_value(item, "source_vacancy_id")),
        url=url or "",
        title=title or "",
        description=_first_text(item, "description", "truncate_description"),
        company=company,
        compensation=_compensation(item),
        employment_type=_optional_text(_value(item, "employment_type")),
        experience_months=_optional_number(_value(item, "experience_months")),
        locations=_locations(_value(item, "location_countries"), _value(item, "location_cities")),
        applicant_locations=_applicant_locations(_value(item, "applicant_locations")),
        category=_optional_text(_value(item, "job_category")),
        industry=_optional_text(_value(item, "industry")),
        tags=_text_values(_value(item, "tags")),
        language_requirements=_language_requirements(_value(item, "raw_djinni_metadata")),
        published_at=published_at,
        valid_through=_parse_source_date(_value(item, "valid_through"), "valid_through", logger),
        scraped_at=scraped_at or datetime.now(timezone.utc),
        discovery_profiles=_text_values(_value(item, "category")),
        raw_source_metadata=_raw_source_metadata(item),
    )


def _value(item: Mapping[str, Any], key: str) -> Any:
    return item.get(key)


def _first_text(item: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _optional_text(_value(item, key))
        if value is not None:
            return value
    return None


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.casefold() in LEGACY_MISSING_VALUES:
        return None
    return text


def _optional_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        if not math.isfinite(parsed):
            return None
        return int(parsed) if parsed.is_integer() else parsed
    return None


def _normalize_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _compensation(item: Mapping[str, Any]) -> Compensation | None:
    compensation = Compensation(
        min_amount=_optional_number(_value(item, "salary_min")),
        max_amount=_optional_number(_value(item, "salary_max")),
        currency=_optional_text(_value(item, "salary_currency")),
        unit=_optional_text(_value(item, "salary_unit")),
    )
    return (
        compensation
        if any(
            value is not None
            for value in (
                compensation.min_amount,
                compensation.max_amount,
                compensation.currency,
                compensation.unit,
            )
        )
        else None
    )


def _text_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        return ()
    return tuple(text for value in values if (text := _optional_text(value)) is not None)


def _locations(countries_value: Any, cities_value: Any) -> tuple[Location, ...]:
    countries = _text_values(countries_value)
    cities = _text_values(cities_value)
    return tuple(
        Location(country=country, city=city)
        for country, city in zip_longest(countries, cities)
        if country is not None or city is not None
    )


def _applicant_locations(value: Any) -> tuple[Location, ...]:
    requirements = (value,) if isinstance(value, Mapping) else value if isinstance(value, (list, tuple)) else ()
    locations = []
    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            continue
        address = requirement.get("address")
        if not isinstance(address, Mapping):
            continue
        countries = _text_values(address.get("addressCountry"))
        cities = _text_values(address.get("addressLocality"))
        regions = _text_values(address.get("addressRegion"))
        for country, city, region in zip_longest(countries, cities, regions):
            if country is not None or city is not None or region is not None:
                locations.append(Location(country=country, city=city, region=region))
    return tuple(locations)


def _language_requirements(value: Any) -> tuple[LanguageRequirement, ...]:
    if not isinstance(value, Mapping):
        return ()
    requirements = value.get("language_requirements")
    if not isinstance(requirements, (list, tuple)):
        return ()
    result = []
    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            continue
        language = _optional_text(requirement.get("language"))
        if language:
            result.append(LanguageRequirement(language=language, level=_optional_text(requirement.get("level"))))
    return tuple(result)


def _parse_source_date(value: Any, field_name: str, logger: logging.Logger) -> date | datetime | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        if "T" in text or " " in text:
            normalized_text = text.removesuffix("Z") + "+00:00" if text.endswith("Z") else text
            return datetime.fromisoformat(normalized_text)
        return date.fromisoformat(text)
    except ValueError:
        logger.warning("Ignoring malformed optional Djinni %s: %r", field_name, text)
        return None


def _parse_listing_date(value: Any, logger: logging.Logger) -> datetime | None:
    text = _optional_text(value)
    if text is None:
        return None
    for pattern in ("%H:%M %d.%m.%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    logger.warning("Ignoring unparseable legacy Djinni pub_date: %r", text)
    return None


def _raw_source_metadata(item: Mapping[str, Any]) -> dict[str, Any]:
    metadata = {}
    raw_jsonld = _value(item, "raw_jsonld")
    raw_site_metadata = _value(item, "raw_djinni_metadata")
    if raw_jsonld is not None:
        metadata["jsonld"] = raw_jsonld
    if isinstance(raw_site_metadata, Mapping) and raw_site_metadata:
        metadata["site"] = dict(raw_site_metadata)
    return metadata

"""Canonical normalization for public DOU HTML vacancy evidence."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from djinni_scraper.domain.vacancy import Company, Compensation, Location, Vacancy


DOU_SOURCE = "dou"
_VACANCY_ID_RE = re.compile(r"/vacancies/(?P<id>\d+)(?:/|$)")
_UKRAINIAN_MONTHS = {
    "січня": 1,
    "лютого": 2,
    "березня": 3,
    "квітня": 4,
    "травня": 5,
    "червня": 6,
    "липня": 7,
    "серпня": 8,
    "вересня": 9,
    "жовтня": 10,
    "листопада": 11,
    "грудня": 12,
}
_REMOTE_LOCATION_TEXT = frozenset({"віддалено", "remote", "за кордоном", "relocation"})
_CURRENCY_BY_SYMBOL = {"$": "USD", "€": "EUR", "₴": "UAH"}
_AMOUNT = r"(?P<{name}>\d+(?:[\s,]\d{{3}})*(?:\.\d+)?)"
_RANGE_RE = re.compile(
    rf"^\s*(?P<currency>[$€₴])\s*{_AMOUNT.format(name='minimum')}\s*[–—-]\s*{_AMOUNT.format(name='maximum')}\s*$"
)
_FROM_RE = re.compile(rf"^\s*від\s*(?P<currency>[$€₴])\s*{_AMOUNT.format(name='minimum')}\s*$", re.I)
_TO_RE = re.compile(rf"^\s*до\s*(?P<currency>[$€₴])\s*{_AMOUNT.format(name='maximum')}\s*$", re.I)
_DATE_RE = re.compile(r"^\s*(?P<day>\d{1,2})\s+(?P<month>[А-Яа-яІіЇїЄєҐґ]+)\s+(?P<year>\d{4})\s*$")


def normalize_dou_url(value: str) -> str:
    """Removes non-identity URL components while preserving the DOU vacancy path."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("DOU vacancy URL must be a non-empty string")
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("DOU vacancy URL must be an absolute HTTP URL")
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def dou_vacancy_id_from_url(value: str) -> str | None:
    """Extracts DOU's stable numeric vacancy segment independently of company slug."""
    try:
        path = urlsplit(value).path
    except (TypeError, ValueError):
        return None
    match = _VACANCY_ID_RE.search(path)
    return match.group("id") if match else None


def parse_dou_publication_date(value: object) -> date | None:
    """Parses only complete Ukrainian day/month/year publication dates."""
    if not isinstance(value, str):
        return None
    match = _DATE_RE.match(" ".join(value.replace("\xa0", " ").split()))
    if match is None:
        return None
    month = _UKRAINIAN_MONTHS.get(match.group("month").casefold())
    if month is None:
        return None
    try:
        return date(int(match.group("year")), month, int(match.group("day")))
    except ValueError:
        return None


def parse_dou_compensation(value: object) -> Compensation | None:
    """Conservatively supports unambiguous DOU numeric salary forms only."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.replace("\xa0", " ").split())
    for pattern, minimum_name, maximum_name in (
        (_RANGE_RE, "minimum", "maximum"),
        (_FROM_RE, "minimum", None),
        (_TO_RE, None, "maximum"),
    ):
        match = pattern.match(text)
        if match is None:
            continue
        minimum = _parse_amount(match.group(minimum_name)) if minimum_name else None
        maximum = _parse_amount(match.group(maximum_name)) if maximum_name else None
        if (minimum_name and minimum is None) or (maximum_name and maximum is None):
            return None
        return Compensation(
            min_amount=minimum,
            max_amount=maximum,
            currency=_CURRENCY_BY_SYMBOL[match.group("currency")],
        )
    return None


def parse_dou_locations(value: object) -> tuple[Location, ...]:
    """Keeps explicit city labels while refusing to turn remote text into a city."""
    if not isinstance(value, str):
        return ()
    locations: list[Location] = []
    seen: set[str] = set()
    for raw_part in value.replace("\xa0", " ").split(","):
        city = " ".join(raw_part.split())
        if not city or city.casefold() in _REMOTE_LOCATION_TEXT:
            continue
        if city.casefold() not in seen:
            locations.append(Location(city=city))
            seen.add(city.casefold())
    return tuple(locations)


def normalize_dou_vacancy(
    item: Mapping[str, Any], *, scraped_at: datetime | None = None
) -> Vacancy:
    """Builds a canonical DOU ``Vacancy`` from listing/detail parser evidence."""
    canonical_url = normalize_dou_url(_preferred_text(item, "canonical_url", "url") or "")
    source_vacancy_id = dou_vacancy_id_from_url(canonical_url)
    if source_vacancy_id is None:
        raise ValueError("DOU vacancy URL does not contain a stable numeric /vacancies/ ID")

    detail_date = _optional_text(item.get("detail_date_text"))
    listing_date = _optional_text(item.get("listing_date_text"))
    detail_salary = _optional_text(item.get("detail_salary_text"))
    listing_salary = _optional_text(item.get("listing_salary_text"))
    detail_locations = _optional_text(item.get("detail_locations_text"))
    listing_locations = _optional_text(item.get("listing_locations_text"))
    detail_category = _optional_text(item.get("detail_category"))
    listing_category = _optional_text(item.get("listing_category"))
    listing_profile = _optional_text(item.get("listing_profile"))

    company_name = _preferred_text(item, "company", "listing_company")
    company_url = _optional_text(item.get("company_url"))
    company = Company(name=company_name, url=company_url) if company_name or company_url else None
    description = _preferred_text(item, "detail_description_html", "listing_description")
    title = _preferred_text(item, "detail_title", "title")
    if title is None:
        raise ValueError("DOU vacancy has no title in detail or listing evidence")
    if description is None:
        raise ValueError("DOU vacancy has no detail or listing description evidence")

    raw_metadata = {
        "listing": {
            "date_text": listing_date,
            "salary_text": listing_salary,
            "locations_text": listing_locations,
            "description_preview": _optional_text(item.get("listing_description")),
            "category": listing_category,
            "profile": listing_profile,
        },
        "detail": {
            "date_text": detail_date,
            "salary_text": detail_salary,
            "locations_text": detail_locations,
            "category": detail_category,
            "filter_links": _json_safe_filter_links(item.get("detail_filter_links")),
        },
    }
    extra_metadata = item.get("raw_dou_metadata")
    if isinstance(extra_metadata, Mapping):
        raw_metadata["source"] = _json_safe_mapping(extra_metadata)

    timestamp = scraped_at or datetime.now(timezone.utc)
    return Vacancy(
        source=DOU_SOURCE,
        source_vacancy_id=source_vacancy_id,
        url=canonical_url,
        title=title,
        description=description,
        company=company,
        compensation=parse_dou_compensation(detail_salary or listing_salary),
        experience_months=None,
        locations=parse_dou_locations(detail_locations or listing_locations),
        category=detail_category or listing_category,
        published_at=parse_dou_publication_date(detail_date or listing_date),
        scraped_at=timestamp,
        discovery_profiles=(listing_profile,) if listing_profile else (),
        raw_source_metadata=raw_metadata,
    )


def _parse_amount(value: str | None) -> int | float | None:
    if value is None:
        return None
    normalized = value.replace(" ", "").replace(",", "")
    try:
        number = float(normalized)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _preferred_text(item: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        text = _optional_text(item.get(name))
        if text is not None:
            return text
    return None


def _json_safe_filter_links(value: object) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    links = []
    for link in value:
        if not isinstance(link, Mapping):
            continue
        href = _optional_text(link.get("href"))
        text = _optional_text(link.get("text"))
        if href or text:
            links.append({"href": href or "", "text": text or ""})
    return links


def _json_safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            safe[str(key)] = _json_safe_mapping(item)
        elif isinstance(item, (list, tuple)):
            safe[str(key)] = [entry for entry in item if entry is None or isinstance(entry, (str, int, float, bool))]
        elif item is None or isinstance(item, (str, int, float, bool)):
            safe[str(key)] = item
    return safe

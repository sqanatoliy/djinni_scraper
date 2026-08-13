"""Pure helpers for Djinni vacancy detail pages and their JSON-LD metadata."""

import json
import logging
import re
from collections.abc import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit


LOGGER = logging.getLogger(__name__)
JOB_POSTING_TYPE = "JobPosting"
JOB_ID_PATTERN = re.compile(r"/jobs/(\d+)(?:[-/]|$)")
DETAIL_METADATA_SCALAR_FIELDS = (
    "source_vacancy_id",
    "detail_title",
    "canonical_url",
    "date_posted",
    "valid_through",
    "salary_currency",
    "salary_min",
    "salary_max",
    "salary_unit",
    "employment_type",
    "experience_months",
    "company_name",
    "company_url",
    "industry",
    "job_category",
    "description",
    "raw_jsonld",
)


def find_job_posting(jsonld_blocks: Iterable[str], detail_url: str | None = None, logger=None):
    """Returns one deterministic JobPosting object from all JSON-LD script blocks."""
    logger = logger or LOGGER
    candidates = []

    for index, block in enumerate(jsonld_blocks, start=1):
        try:
            payload = json.loads(block)
        except (TypeError, json.JSONDecodeError):
            logger.warning("Skipping malformed Djinni JSON-LD block #%s.", index)
            continue

        candidates.extend(node for node in _iter_jsonld_nodes(payload) if _is_job_posting(node))

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    selected = _select_matching_job_posting(candidates, detail_url)
    logger.warning(
        "Found %s Djinni JobPosting objects on %s; selected one by URL/identifier match or document order.",
        len(candidates),
        detail_url or "the response",
    )
    return selected


def parse_detail_metadata(response, logger=None) -> dict:
    """Extracts optional detail metadata without coupling it to persistence."""
    logger = logger or LOGGER
    job_posting = find_job_posting(
        response.css('script[type="application/ld+json"]::text').getall(),
        detail_url=response.url,
        logger=logger,
    )
    raw_djinni_metadata = _extract_dom_metadata(response)

    if not job_posting:
        logger.warning("Djinni detail page has no valid JobPosting JSON-LD: %s", response.url)
        return {
            **_empty_detail_metadata(),
            "raw_jsonld": None,
            "raw_djinni_metadata": raw_djinni_metadata,
        }

    salary = _extract_salary(job_posting.get("baseSalary"))
    locations = _extract_job_locations(job_posting.get("jobLocation"))
    canonical_url = _canonical_url(job_posting.get("url"), response.url)

    return {
        **_empty_detail_metadata(),
        "source_vacancy_id": _extract_identifier(job_posting.get("identifier"))
        or _extract_job_id_from_url(canonical_url or response.url),
        "detail_title": _optional_text(job_posting.get("title")),
        "canonical_url": canonical_url,
        "date_posted": _optional_text(job_posting.get("datePosted")),
        "valid_through": _optional_text(job_posting.get("validThrough")),
        "salary_currency": salary["currency"],
        "salary_min": salary["min"],
        "salary_max": salary["max"],
        "salary_unit": salary["unit"],
        "employment_type": _optional_text(job_posting.get("employmentType")),
        "experience_months": _extract_experience_months(job_posting.get("experienceRequirements")),
        "company_name": _optional_text(_mapping_value(job_posting.get("hiringOrganization"), "name")),
        "company_url": _optional_text(_mapping_value(job_posting.get("hiringOrganization"), "sameAs")),
        "industry": _optional_text(job_posting.get("industry")),
        "job_category": _optional_text(job_posting.get("category")),
        "location_countries": locations["countries"],
        "location_cities": locations["cities"],
        "applicant_locations": _as_list_of_mappings(job_posting.get("applicantLocationRequirements")),
        "description": _optional_text(job_posting.get("description")),
        "raw_jsonld": job_posting,
        "raw_djinni_metadata": raw_djinni_metadata,
    }


def _empty_detail_metadata() -> dict:
    """Returns fresh JSON-serializable missing values for one detail response."""
    return {
        **dict.fromkeys(DETAIL_METADATA_SCALAR_FIELDS),
        "location_countries": [],
        "location_cities": [],
        "applicant_locations": [],
        "raw_djinni_metadata": {},
    }


def _iter_jsonld_nodes(value):
    """Yields mapping nodes from direct objects, arrays, @graph and nested JSON-LD objects."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _iter_jsonld_nodes(child)
    elif isinstance(value, list):
        for child in value:
            if isinstance(child, (dict, list)):
                yield from _iter_jsonld_nodes(child)


def _is_job_posting(value) -> bool:
    if not isinstance(value, dict):
        return False
    types = value.get("@type")
    if isinstance(types, str):
        return types == JOB_POSTING_TYPE
    return isinstance(types, list) and JOB_POSTING_TYPE in types


def _select_matching_job_posting(candidates: list[dict], detail_url: str | None) -> dict:
    if detail_url:
        target_url = _normalize_url(detail_url)
        for candidate in candidates:
            candidate_url = candidate.get("url")
            if isinstance(candidate_url, str) and _normalize_url(urljoin(detail_url, candidate_url)) == target_url:
                return candidate

        target_id = _extract_job_id_from_url(detail_url)
        if target_id:
            for candidate in candidates:
                if _extract_identifier(candidate.get("identifier")) == target_id:
                    return candidate
    return candidates[0]


def _extract_identifier(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, (str, int)):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return None


def _extract_job_id_from_url(url: str | None) -> str | None:
    if not isinstance(url, str):
        return None
    match = JOB_ID_PATTERN.search(urlsplit(url).path)
    return match.group(1) if match else None


def _canonical_url(value, response_url: str) -> str | None:
    if isinstance(value, str) and value.strip():
        return _normalize_url(urljoin(response_url, value.strip()))
    return _normalize_url(response_url) if _extract_job_id_from_url(response_url) else None


def _normalize_url(value: str) -> str:
    parsed = urlsplit(value)
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _extract_salary(value) -> dict:
    if not isinstance(value, dict):
        return {"currency": None, "min": None, "max": None, "unit": None}

    salary_value = value.get("value")
    if not isinstance(salary_value, dict):
        salary_value = {}
    return {
        "currency": _optional_text(value.get("currency")),
        "min": _numeric_value(salary_value.get("minValue")),
        "max": _numeric_value(salary_value.get("maxValue")),
        "unit": _optional_text(salary_value.get("unitText")),
    }


def _extract_experience_months(value):
    if not isinstance(value, dict):
        return None
    return _numeric_value(value.get("monthsOfExperience"))


def _extract_job_locations(value) -> dict:
    countries = []
    cities = []
    for location in _as_list_of_mappings(value):
        address = location.get("address")
        if not isinstance(address, dict):
            continue
        countries.extend(_text_values(address.get("addressCountry")))
        cities.extend(_text_values(address.get("addressLocality")))
    return {"countries": _unique(countries), "cities": _unique(cities)}


def _extract_dom_metadata(response) -> dict:
    languages = []
    for language in response.css(".csc--language"):
        name = _selector_text(language.css(".csc__primary::text").get())
        level = _selector_text(language.css(".csc__secondary::text").get())
        if name:
            languages.append({"language": name, "level": level})

    metadata = {
        "language_requirements": languages,
        "publication_info": _selector_text(response.css("#job-publication-info ::text").getall()),
        "extra_info": [
            text
            for text in (_selector_text(value) for value in response.css("#job_extra_info .fw-medium::text").getall())
            if text
        ],
    }
    return {key: value for key, value in metadata.items() if value}


def _as_list_of_mappings(value) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _mapping_value(value, key):
    return value.get(key) if isinstance(value, dict) else None


def _numeric_value(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return int(parsed) if parsed.is_integer() else parsed
    return None


def _text_values(value) -> list[str]:
    if isinstance(value, list):
        return [text for item in value for text in _text_values(item)]
    text = _optional_text(value)
    return [text] if text else []


def _optional_text(value) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _selector_text(value) -> str | None:
    if isinstance(value, list):
        value = " ".join(value)
    return " ".join(value.split()) if isinstance(value, str) and value.split() else None


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))

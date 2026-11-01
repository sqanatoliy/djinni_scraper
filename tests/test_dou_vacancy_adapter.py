from datetime import date, datetime, timezone

import pytest

from djinni_scraper.adapters.dou_vacancy import (
    dou_vacancy_id_from_url,
    normalize_dou_url,
    normalize_dou_vacancy,
    parse_dou_compensation,
    parse_dou_locations,
    parse_dou_publication_date,
)
from djinni_scraper.domain.vacancy import Compensation, Location


SCRAPED_AT = datetime(2026, 10, 13, 10, 30, tzinfo=timezone.utc)
DETAIL_URL = "https://jobs.dou.ua/companies/acme/vacancies/346075/"


def _item(**overrides):
    values = {
        "title": "Listing title",
        "url": DETAIL_URL,
        "company": "Listing company",
        "listing_date_text": "12 жовтня 2026",
        "listing_salary_text": "$900–1100",
        "listing_locations_text": "Львів, віддалено",
        "listing_description": "Listing preview",
        "listing_category": "Search category",
        "listing_profile": "dou-data-engineer-0-1",
        "detail_title": "Detail title",
        "detail_date_text": "13 жовтня 2026",
        "detail_salary_text": "до $1500",
        "detail_locations_text": "Київ, віддалено",
        "detail_description_html": "<p>Full DOU description</p>",
        "detail_category": "Data Engineer",
        "detail_filter_links": [{"text": "Data Engineer", "href": "/vacancies/?category=Data+Engineer"}],
        "canonical_url": "https://jobs.dou.ua/companies/acme/vacancies/346075/?from=listing#top",
        "company_url": "https://jobs.dou.ua/companies/acme/",
        "raw_dou_metadata": {"detail_url": DETAIL_URL},
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("url", "expected"),
    (
        ("https://jobs.dou.ua/companies/acme/vacancies/346075/", "346075"),
        ("https://jobs.dou.ua/companies/renamed/vacancies/346075/?from=feed#x", "346075"),
        ("https://jobs.dou.ua/vacancies/not-numeric/", None),
        ("not a URL", None),
    ),
)
def test_dou_numeric_identity_is_independent_of_company_slug(url, expected):
    assert dou_vacancy_id_from_url(url) == expected


def test_canonical_url_strips_query_fragment_and_trailing_slash():
    assert normalize_dou_url("HTTPS://Jobs.DOU.UA/companies/acme/vacancies/346075/?from=a#frag") == (
        "https://jobs.dou.ua/companies/acme/vacancies/346075"
    )


@pytest.mark.parametrize(
    ("month", "number"),
    (
        ("січня", 1),
        ("лютого", 2),
        ("березня", 3),
        ("квітня", 4),
        ("травня", 5),
        ("червня", 6),
        ("липня", 7),
        ("серпня", 8),
        ("вересня", 9),
        ("жовтня", 10),
        ("листопада", 11),
        ("грудня", 12),
    ),
)
def test_publication_parser_supports_each_ukrainian_month(month, number):
    assert parse_dou_publication_date(f"13 {month} 2026") == date(2026, number, 13)


@pytest.mark.parametrize("value", (None, "", "13 жовтня", "жовтня 2026", "31 лютого 2026", "13 October 2026"))
def test_publication_parser_does_not_invent_a_year_or_accept_malformed_dates(value):
    assert parse_dou_publication_date(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("$1000–2000", Compensation(min_amount=1000, max_amount=2000, currency="USD")),
        ("від $1200", Compensation(min_amount=1200, currency="USD")),
        ("до €1 500", Compensation(max_amount=1500, currency="EUR")),
        ("$1000 - $2000", None),
        ("обговорюється", None),
        (None, None),
    ),
)
def test_compensation_parser_only_maps_unambiguous_supported_forms(value, expected):
    assert parse_dou_compensation(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("Київ", (Location(city="Київ"),)),
        ("Київ, Львів", (Location(city="Київ"), Location(city="Львів"))),
        ("віддалено, remote, за кордоном", ()),
        (None, ()),
    ),
)
def test_locations_keep_only_explicit_city_evidence(value, expected):
    assert parse_dou_locations(value) == expected


def test_normalizer_prefers_detail_evidence_and_preserves_raw_provenance_without_fake_fields():
    vacancy = normalize_dou_vacancy(_item(), scraped_at=SCRAPED_AT)

    assert vacancy.source == "dou"
    assert vacancy.source_vacancy_id == "346075"
    assert vacancy.url == "https://jobs.dou.ua/companies/acme/vacancies/346075"
    assert vacancy.title == "Detail title"
    assert vacancy.description == "<p>Full DOU description</p>"
    assert vacancy.company.name == "Listing company"
    assert vacancy.company.url == "https://jobs.dou.ua/companies/acme/"
    assert vacancy.compensation == Compensation(max_amount=1500, currency="USD")
    assert vacancy.locations == (Location(city="Київ"),)
    assert vacancy.published_at == date(2026, 10, 13)
    assert vacancy.category == "Data Engineer"
    assert vacancy.discovery_profiles == ("dou-data-engineer-0-1",)
    assert vacancy.experience_months is None
    assert vacancy.employment_type is None
    assert vacancy.language_requirements == ()
    assert vacancy.raw_source_metadata["listing"]["profile"] == "dou-data-engineer-0-1"
    assert vacancy.raw_source_metadata["detail"]["date_text"] == "13 жовтня 2026"
    assert vacancy.raw_source_metadata["source"]["detail_url"] == DETAIL_URL
    assert vacancy.scraped_at == SCRAPED_AT


def test_same_numeric_id_with_a_changed_company_slug_has_same_canonical_identity():
    original = normalize_dou_vacancy(_item(), scraped_at=SCRAPED_AT)
    renamed = normalize_dou_vacancy(
        _item(canonical_url="https://jobs.dou.ua/companies/new-acme/vacancies/346075/"),
        scraped_at=SCRAPED_AT,
    )

    assert original.source_vacancy_id == renamed.source_vacancy_id == "346075"
    assert original.url != renamed.url


@pytest.mark.parametrize(
    "item",
    (
        {"url": DETAIL_URL, "detail_description_html": "<p>x</p>"},
        {"url": DETAIL_URL, "detail_title": "Title"},
        {"url": "https://jobs.dou.ua/companies/acme/vacancies/not-a-number/", "detail_title": "Title", "detail_description_html": "<p>x</p>"},
    ),
)
def test_normalizer_rejects_missing_required_identity_title_or_detail_content(item):
    with pytest.raises(ValueError):
        normalize_dou_vacancy(item, scraped_at=SCRAPED_AT)

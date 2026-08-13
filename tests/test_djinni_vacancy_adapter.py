from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from scrapy.http import HtmlResponse

from djinni_scraper.adapters.djinni_vacancy import normalize_djinni_vacancy
from djinni_scraper.items import DjinniScraperItem
from djinni_scraper.parsers.djinni_jsonld import parse_detail_metadata


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "djinni"
DETAIL_URL = "https://djinni.co/jobs/842583-strong-junior-middle-ai-engineer/"
SCRAPED_AT = datetime(2026, 8, 13, 9, 30, tzinfo=timezone.utc)


def full_djinni_item() -> DjinniScraperItem:
    response = HtmlResponse(
        url=DETAIL_URL,
        body=(FIXTURES_DIR / "job_detail_full.html").read_bytes(),
        encoding="utf-8",
    )
    item = DjinniScraperItem(
        title="Listing title",
        salary="$$",
        company="Listing company",
        views="Not specified",
        responses="Not specified",
        pub_date="14:01 12.08.2026",
        truncate_description="Listing description",
        tags=["Продуктова компанія"],
        url=DETAIL_URL,
        category="python",
    )
    item.update(parse_detail_metadata(response))
    return item


def test_full_djinni_item_normalizes_real_fixture_with_explicit_semantics():
    vacancy = normalize_djinni_vacancy(full_djinni_item(), scraped_at=SCRAPED_AT)

    assert vacancy.source == "djinni"
    assert vacancy.source_vacancy_id == "842583"
    assert vacancy.url == "https://djinni.co/jobs/842583-strong-junior-middle-ai-engineer"
    assert vacancy.title == "Strong Junior/ Middle AI Engineer"
    assert vacancy.description.startswith("Softcery builds advanced AI systems")
    assert vacancy.company.name == "Softcery – Production AI Systems"
    assert vacancy.company.url == "https://softcery.com/"
    assert vacancy.compensation.min_amount == 1200
    assert vacancy.compensation.max_amount == 2000
    assert vacancy.compensation.currency == "USD"
    assert vacancy.compensation.unit == "MONTH"
    assert vacancy.employment_type == "FULL_TIME"
    assert vacancy.experience_months == 12
    assert vacancy.locations[0].country == "Україна"
    assert vacancy.locations[0].city == "Львів"
    assert vacancy.applicant_locations[0].region == "Europe"
    assert vacancy.category == "Python"
    assert vacancy.industry == "other"
    assert vacancy.tags == ("Продуктова компанія",)
    assert vacancy.discovery_profiles == ("python",)
    assert vacancy.language_requirements[0].language == "Англійська"
    assert vacancy.raw_source_metadata["jsonld"]["identifier"] == 842583
    assert vacancy.raw_source_metadata["site"]["extra_info"] == ("Python",)
    assert vacancy.published_at == datetime(2026, 8, 12, 14, 1, 42, 527603)
    assert vacancy.valid_through == datetime(2026, 9, 11, 14, 1, 42, 527603)
    assert vacancy.scraped_at == SCRAPED_AT


def test_url_title_description_and_company_follow_structured_then_listing_precedence():
    item = full_djinni_item()
    item.update(
        {
            "canonical_url": "https://djinni.co/jobs/99-detail/?utm=ignored",
            "detail_title": "Detail title",
            "description": "Detail description",
            "company_name": "Detail company",
            "company_url": "https://company.invalid/",
        }
    )

    vacancy = normalize_djinni_vacancy(item, scraped_at=SCRAPED_AT)
    assert vacancy.url == "https://djinni.co/jobs/99-detail"
    assert vacancy.title == "Detail title"
    assert vacancy.description == "Detail description"
    assert vacancy.company.name == "Detail company"

    item.update(
        {
            "canonical_url": None,
            "detail_title": "Not specified",
            "description": "",
            "company_name": None,
            "company_url": None,
        }
    )
    fallback = normalize_djinni_vacancy(item, scraped_at=SCRAPED_AT)
    assert fallback.url == DETAIL_URL.rstrip("/")
    assert fallback.title == "Listing title"
    assert fallback.description == "Listing description"
    assert fallback.company.name == "Listing company"


def test_structured_salary_is_used_without_guessing_legacy_salary_string():
    item = full_djinni_item()
    item.update({"salary_min": None, "salary_max": None, "salary_currency": None, "salary_unit": None, "salary": "$$"})

    assert normalize_djinni_vacancy(item, scraped_at=SCRAPED_AT).compensation is None


def test_legacy_placeholders_and_malformed_optional_values_degrade_safely(caplog):
    item = full_djinni_item()
    item.update(
        {
            "source_vacancy_id": "Not specified",
            "experience_months": "unknown",
            "date_posted": "not-a-date",
            "valid_through": "also-not-a-date",
            "job_category": "Not specified",
            "industry": "",
            "tags": ["Not specified", "  "],
            "category": "Not specified",
            "raw_djinni_metadata": {"language_requirements": [{"language": "", "level": "B2"}]},
        }
    )

    vacancy = normalize_djinni_vacancy(item, scraped_at=SCRAPED_AT)
    assert vacancy.source_vacancy_id is None
    assert vacancy.experience_months is None
    assert vacancy.published_at == datetime(2026, 8, 12, 14, 1)
    assert vacancy.valid_through is None
    assert vacancy.category is None
    assert vacancy.industry is None
    assert vacancy.tags == ()
    assert vacancy.discovery_profiles == ()
    assert vacancy.language_requirements == ()
    assert "malformed optional Djinni date_posted" in caplog.text
    assert "malformed optional Djinni valid_through" in caplog.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-12", date(2026, 8, 12)),
        ("2026-08-12T14:01:42+03:00", "2026-08-12T14:01:42+03:00"),
    ],
)
def test_date_parser_preserves_date_only_and_timezone_aware_values(value, expected):
    item = full_djinni_item()
    item["date_posted"] = value

    published_at = normalize_djinni_vacancy(item, scraped_at=SCRAPED_AT).published_at
    if isinstance(expected, str):
        assert published_at.isoformat() == expected
    else:
        assert published_at == expected


def test_adapter_requires_a_meaningful_url_and_title_and_allows_deterministic_scraped_at():
    with pytest.raises(ValueError, match="url"):
        normalize_djinni_vacancy({"title": "Role"}, scraped_at=SCRAPED_AT)
    with pytest.raises(ValueError, match="title"):
        normalize_djinni_vacancy({"url": DETAIL_URL}, scraped_at=SCRAPED_AT)
    with pytest.raises(ValueError, match="timezone-aware"):
        normalize_djinni_vacancy({"url": DETAIL_URL, "title": "Role"}, scraped_at=datetime(2026, 8, 13))

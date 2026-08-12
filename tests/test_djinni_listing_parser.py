import os
from pathlib import Path

os.environ.setdefault("DJINNI_EMAIL", "test@example.invalid")
os.environ.setdefault("DJINNI_PASSWORD", "not-a-real-password")

import pytest
from scrapy.http import HtmlResponse

from djinni_scraper.spiders.djinni import DjinniSelectors, DjinniSpider


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "djinni"
LISTING_URL = "https://djinni.co/jobs/?primary_keyword=Python&exp_level=1y"


def load_response(filename: str) -> HtmlResponse:
    return HtmlResponse(
        url=LISTING_URL,
        body=(FIXTURES_DIR / filename).read_bytes(),
        encoding="utf-8",
    )


@pytest.fixture
def listing_response() -> HtmlResponse:
    return load_response("jobs_listing.html")


@pytest.fixture
def spider() -> DjinniSpider:
    return DjinniSpider(primary_keyword="Python")


def test_listing_fixture_has_expected_current_cards(listing_response):
    assert len(listing_response.css(DjinniSelectors.JOBS_LIST_SELECTOR)) == 3


def test_parses_title_company_salary_description_url_and_publication_date(spider, listing_response):
    item = spider._parse_job_card(listing_response.css(DjinniSelectors.JOBS_LIST_SELECTOR)[0], listing_response)

    assert dict(item) == {
        "title": "Strong Junior/ Middle AI Engineer",
        "salary": "до $2000",
        "company": "Softcery – Production AI Systems",
        "views": "Not specified",
        "responses": "Not specified",
        "pub_date": "14:01 12.08.2026",
        "truncate_description": "Softcery builds advanced AI systems. Join our production AI team...",
        "tags": [],
        "url": "https://djinni.co/jobs/842583-strong-junior-middle-ai-engineer/",
        "category": "Python",
    }


def test_parses_raw_tags_and_keeps_salary_fallback_when_only_salary_level_is_shown(spider, listing_response):
    item = spider._parse_job_card(listing_response.css(DjinniSelectors.JOBS_LIST_SELECTOR)[1], listing_response)

    assert item["title"] == "Junior AI Automation Specialist"
    assert item["company"] == "iPlayTech"
    assert item["salary"] == "Not specified"
    assert item["truncate_description"] == "Join a fast-growing international iGaming company with modern AI tools..."
    assert item["tags"] == ["Продуктова компанія"]
    assert item["pub_date"] == "12:16 11.08.2026"


def test_listing_metadata_without_semantic_hooks_uses_current_safe_fallback(spider, listing_response):
    item = spider._parse_job_card(listing_response.css(DjinniSelectors.JOBS_LIST_SELECTOR)[2], listing_response)

    assert item["views"] == "Not specified"
    assert item["responses"] == "Not specified"
    assert item["pub_date"] == "10:18 12.08.2026"


def test_pagination_returns_absolute_next_page_url(spider, listing_response):
    assert spider._get_next_page_url(listing_response) == (
        "https://djinni.co/jobs/?primary_keyword=Python&exp_level=1y&page=2"
    )


def test_pagination_returns_none_on_last_page(spider):
    assert spider._get_next_page_url(load_response("jobs_listing_last_page.html")) is None

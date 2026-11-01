import asyncio
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


def test_parses_listing_transport_fields_for_detail_enrichment(spider, listing_response):
    item = spider._parse_job_card(listing_response.css(DjinniSelectors.JOBS_LIST_SELECTOR)[0], listing_response)

    assert dict(item) == {
        "title": "Strong Junior/ Middle AI Engineer",
        "company": "Softcery – Production AI Systems",
        "listing_published_at": "14:01 12.08.2026",
        "listing_description": "Softcery builds advanced AI systems. Join our production AI team...",
        "tags": [],
        "url": "https://djinni.co/jobs/842583-strong-junior-middle-ai-engineer/",
        "discovery_profile": "Python",
    }


def test_parses_raw_tags_and_listing_fallback_fields(spider, listing_response):
    item = spider._parse_job_card(listing_response.css(DjinniSelectors.JOBS_LIST_SELECTOR)[1], listing_response)

    assert item["title"] == "Junior AI Automation Specialist"
    assert item["company"] == "iPlayTech"
    assert item["listing_description"] == "Join a fast-growing international iGaming company with modern AI tools..."
    assert item["tags"] == ["Продуктова компанія"]
    assert item["listing_published_at"] == "12:16 11.08.2026"


def test_listing_publication_metadata_uses_current_safe_fallback(spider, listing_response):
    item = spider._parse_job_card(listing_response.css(DjinniSelectors.JOBS_LIST_SELECTOR)[2], listing_response)

    assert item["listing_published_at"] == "10:18 12.08.2026"


def test_pagination_returns_absolute_next_page_url(spider, listing_response):
    assert spider._get_next_page_url(listing_response) == (
        "https://djinni.co/jobs/?primary_keyword=Python&exp_level=1y&page=2"
    )


def test_pagination_keeps_all_active_profile_filters_when_the_link_is_partial(spider):
    response = HtmlResponse(
        url=(
            "https://djinni.co/jobs/?search_type=basic-search&primary_keyword=Python"
            "&exp_level=no_exp&exp_level=1y&exp_level=2y"
            "&english_level=no_english&english_level=basic"
        ),
        body=(FIXTURES_DIR / "jobs_listing.html").read_bytes(),
        encoding="utf-8",
    )

    assert spider._get_next_page_url(response) == (
        "https://djinni.co/jobs/?search_type=basic-search&primary_keyword=Python"
        "&exp_level=no_exp&exp_level=1y&exp_level=2y"
        "&english_level=no_english&english_level=basic&page=2"
    )


def test_pagination_returns_none_on_last_page(spider):
    assert spider._get_next_page_url(load_response("jobs_listing_last_page.html")) is None


def test_listing_completion_marker_is_emitted_only_after_the_last_page(spider, listing_response):
    completed = []
    spider._record_listing_completed = lambda: completed.append(True)

    async def collect(response):
        return [request async for request in spider.parse_pagination(response)]

    next_page_requests = asyncio.run(collect(listing_response))
    assert len(next_page_requests) == 1
    assert completed == []

    last_page_requests = asyncio.run(collect(load_response("jobs_listing_last_page.html")))
    assert last_page_requests == []
    assert completed == [True]

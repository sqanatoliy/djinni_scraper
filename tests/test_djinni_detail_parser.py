import json
from pathlib import Path

import pytest
from scrapy.http import HtmlResponse

from djinni_scraper.domain.vacancy import Vacancy
from djinni_scraper.items import DjinniScraperItem
from djinni_scraper.parsers.djinni_jsonld import find_job_posting, parse_detail_metadata
from djinni_scraper.pipelines import SQLitePipeline
from djinni_scraper.spiders.djinni import DjinniSelectors, DjinniSpider


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "djinni"
DETAIL_URL = "https://djinni.co/jobs/842583-strong-junior-middle-ai-engineer/"
LISTING_URL = "https://djinni.co/jobs/?primary_keyword=Python&exp_level=1y"
EDGE_CASES = json.loads((FIXTURES_DIR / "job_detail_jsonld_edge_cases.json").read_text())
PERSISTED_LISTING_FIELDS = (
    "title",
    "salary",
    "company",
    "views",
    "responses",
    "pub_date",
    "truncate_description",
    "tags",
    "url",
    "category",
)


def detail_response(filename: str, url: str = DETAIL_URL) -> HtmlResponse:
    return HtmlResponse(url=url, body=(FIXTURES_DIR / filename).read_bytes(), encoding="utf-8")


def listing_item() -> DjinniScraperItem:
    return DjinniScraperItem(
        title="Strong Junior/ Middle AI Engineer",
        salary="до $2000",
        company="Softcery – Production AI Systems",
        views="Not specified",
        responses="Not specified",
        pub_date="14:01 12.08.2026",
        truncate_description="Softcery builds advanced AI systems.",
        tags=[],
        url=DETAIL_URL,
        category="Python",
    )


def test_full_real_dom_fixture_parses_confirmed_jsonld_fields_and_dom_metadata():
    metadata = parse_detail_metadata(detail_response("job_detail_full.html"))

    assert metadata["source_vacancy_id"] == "842583"
    assert metadata["detail_title"] == "Strong Junior/ Middle AI Engineer"
    assert metadata["canonical_url"] == "https://djinni.co/jobs/842583-strong-junior-middle-ai-engineer"
    assert metadata["date_posted"] == "2026-08-12T14:01:42.527603"
    assert metadata["valid_through"] == "2026-09-11T14:01:42.527603"
    assert metadata["salary_currency"] == "USD"
    assert metadata["salary_min"] == 1200
    assert metadata["salary_max"] == 2000
    assert metadata["salary_unit"] == "MONTH"
    assert metadata["employment_type"] == "FULL_TIME"
    assert metadata["experience_months"] == 12
    assert metadata["company_name"] == "Softcery – Production AI Systems"
    assert metadata["company_url"] == "https://softcery.com/"
    assert metadata["industry"] == "other"
    assert metadata["job_category"] == "Python"
    assert metadata["location_countries"] == ["Україна"]
    assert metadata["location_cities"] == ["Львів"]
    assert metadata["applicant_locations"] == [
        {
            "@type": "AdministrativeArea",
            "address": {
                "@type": "PostalAddress",
                "addressCountry": None,
                "addressLocality": None,
                "addressRegion": "Europe",
            },
        }
    ]
    assert metadata["description"].startswith("Softcery builds advanced AI systems")
    assert metadata["direct_apply"] is True
    assert metadata["raw_jsonld"]["identifier"] == 842583
    assert metadata["raw_djinni_metadata"] == {
        "language_requirements": [
            {"language": "Англійська", "level": "B2 – Вище середнього"},
            {"language": "Українська", "level": "Носій мови"},
        ],
        "publication_info": "Опубліковано 12 серпня 196 переглядів 35 відгуків",
        "extra_info": ["Python"],
    }


def test_minimal_fixture_keeps_optional_metadata_missing_and_uses_url_id_fallback():
    metadata = parse_detail_metadata(
        detail_response("job_detail_minimal.html", "https://djinni.co/jobs/812345-minimal-djinni-role/")
    )

    assert metadata["source_vacancy_id"] == "812345"
    assert metadata["detail_title"] == "Minimal Djinni role"
    assert metadata["salary_currency"] is None
    assert metadata["salary_min"] is None
    assert metadata["salary_max"] is None
    assert metadata["employment_type"] is None
    assert metadata["experience_months"] is None
    assert metadata["location_countries"] == []
    assert metadata["location_cities"] == []
    assert metadata["applicant_locations"] == []
    assert metadata["raw_djinni_metadata"] == {}


def test_malformed_jsonld_before_valid_jobposting_is_ignored(caplog):
    metadata = parse_detail_metadata(detail_response("job_detail_malformed_jsonld.html"))

    assert metadata["source_vacancy_id"] == "842583"
    assert metadata["detail_title"] == "Strong Junior/ Middle AI Engineer"
    assert "Skipping malformed Djinni JSON-LD block #1" in caplog.text


@pytest.mark.parametrize(
    ("case", "expected_title"),
    [
        ("direct", "Direct"),
        ("array", "Array"),
        ("graph", "Graph"),
        ("type_list", "Type list"),
    ],
)
def test_jsonld_discovery_supports_expected_structures(case, expected_title):
    assert find_job_posting([json.dumps(EDGE_CASES[case])])["title"] == expected_title


def test_multiple_jobpostings_selects_url_match_and_logs_warning(caplog):
    job_posting = find_job_posting(
        [json.dumps(EDGE_CASES["multiple"])],
        detail_url=DETAIL_URL,
    )

    assert job_posting["identifier"] == 842583
    assert "Found 2 Djinni JobPosting objects" in caplog.text


def test_missing_identifier_and_non_djinni_url_leave_source_id_missing():
    response = HtmlResponse(
        url="https://djinni.co/company/example/",
        body=b'<script type="application/ld+json">{"@type": "JobPosting", "title": "Role"}</script>',
        encoding="utf-8",
    )

    assert parse_detail_metadata(response)["source_vacancy_id"] is None


@pytest.mark.parametrize(
    ("salary", "expected_min", "expected_max"),
    [
        ({"currency": "USD", "value": {"minValue": 1, "maxValue": 2}}, 1, 2),
        ({"currency": "USD", "value": {"minValue": "1"}}, 1, None),
        ({"currency": "USD", "value": {"maxValue": 2}}, None, 2),
        (None, None, None),
        ({"currency": "USD", "value": "not-a-structured-salary"}, None, None),
    ],
)
def test_salary_parser_handles_optional_and_malformed_shapes(salary, expected_min, expected_max):
    payload = {"@type": "JobPosting", "url": DETAIL_URL}
    if salary is not None:
        payload["baseSalary"] = salary
    response = HtmlResponse(
        url=DETAIL_URL,
        body=f'<script type="application/ld+json">{json.dumps(payload)}</script>'.encode(),
        encoding="utf-8",
    )

    metadata = parse_detail_metadata(response)
    assert metadata["salary_min"] == expected_min
    assert metadata["salary_max"] == expected_max


def test_experience_numeric_string_and_location_list_are_parsed():
    response = HtmlResponse(
        url=DETAIL_URL,
        body=(
            b'<script type="application/ld+json">'
            b'{"@type":"JobPosting","url":"https://djinni.co/jobs/842583-strong-junior-middle-ai-engineer/",'
            b'"experienceRequirements":{"monthsOfExperience":"18"},'
            b'"jobLocation":[{"address":{"addressCountry":"Ukraine","addressLocality":"Kyiv"}},'
            b'{"address":{"addressCountry":"Poland","addressLocality":["Warsaw"]}}]}'
            b'</script>'
        ),
        encoding="utf-8",
    )

    metadata = parse_detail_metadata(response)
    assert metadata["experience_months"] == 18
    assert metadata["location_countries"] == ["Ukraine", "Poland"]
    assert metadata["location_cities"] == ["Kyiv", "Warsaw"]


def test_listing_card_creates_plain_scrapy_detail_request_with_preserved_context():
    response = HtmlResponse(
        url=LISTING_URL,
        body=(FIXTURES_DIR / "jobs_listing.html").read_bytes(),
        encoding="utf-8",
    )
    spider = DjinniSpider(primary_keyword="Python")
    item = spider._parse_job_card(response.css(DjinniSelectors.JOBS_LIST_SELECTOR)[0], response)

    request = spider._detail_request(item)
    assert request.url == DETAIL_URL
    assert request.callback == spider.parse_job_detail
    assert request.meta.get("playwright") is None
    assert request.cb_kwargs["listing_item"]["category"] == "Python"
    assert request.cb_kwargs["listing_item"]["truncate_description"] == "Softcery builds advanced AI systems. Join our production AI team..."


def test_detail_response_yields_one_enriched_item_while_preserving_listing_fields():
    spider = DjinniSpider(primary_keyword="Python")
    enriched_item = list(spider.parse_job_detail(detail_response("job_detail_full.html"), listing_item()))[0]

    assert isinstance(enriched_item, DjinniScraperItem)
    assert {field: enriched_item[field] for field in PERSISTED_LISTING_FIELDS} == {
        "title": "Strong Junior/ Middle AI Engineer",
        "salary": "до $2000",
        "company": "Softcery – Production AI Systems",
        "views": "Not specified",
        "responses": "Not specified",
        "pub_date": "14:01 12.08.2026",
        "truncate_description": "Softcery builds advanced AI systems.",
        "tags": [],
        "url": DETAIL_URL,
        "category": "Python",
    }
    assert enriched_item["source_vacancy_id"] == "842583"
    assert enriched_item["description"].startswith("Softcery builds advanced AI systems")
    assert isinstance(enriched_item["canonical_vacancy"], Vacancy)
    assert enriched_item["canonical_snapshot_complete"] is True
    assert enriched_item["canonical_vacancy"].source == "djinni"
    assert enriched_item["canonical_vacancy"].source_vacancy_id == "842583"


def test_detail_without_jobposting_yields_safe_listing_item(caplog):
    spider = DjinniSpider(primary_keyword="Python")
    response = HtmlResponse(url=DETAIL_URL, body=b"<div class='job-post-page'></div>", encoding="utf-8")

    item = list(spider.parse_job_detail(response, listing_item()))[0]
    assert item["title"] == "Strong Junior/ Middle AI Engineer"
    assert item["source_vacancy_id"] is None
    assert item["raw_jsonld"] is None
    assert item["canonical_snapshot_complete"] is False
    assert "no valid JobPosting JSON-LD" in caplog.text


def test_detail_parse_constructs_canonical_vacancy_without_calling_the_pipeline(monkeypatch):
    calls = []

    def fail_if_called(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("parse_job_detail must not invoke the legacy pipeline")

    monkeypatch.setattr(SQLitePipeline, "process_item", fail_if_called)
    spider = DjinniSpider(primary_keyword="Python")

    item = list(spider.parse_job_detail(detail_response("job_detail_full.html"), listing_item()))[0]
    assert isinstance(item["canonical_vacancy"], Vacancy)
    assert calls == []

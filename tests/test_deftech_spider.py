import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
from scrapy import FormRequest, Request
from scrapy.exceptions import CloseSpider
from scrapy.http import HtmlResponse, TextResponse

from djinni_scraper.spiders.deftech_dou import DefTechDouSpider


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "deftech"
LISTING_URL = "https://deftech.dou.ua/jobs/?category=Data+Engineer"
XHR_URL = "https://deftech.dou.ua/jobs/xhr-load/?category=Data%20Engineer"
DETAIL_URL = "https://deftech.dou.ua/jobs/companies/acme/vacancies/374909/"


class _Stats:
    def __init__(self):
        self.values = {}

    def get_value(self, key, default=None):
        return self.values.get(key, default)

    def set_value(self, key, value):
        self.values[key] = value


def _spider():
    spider = DefTechDouSpider(profile="deftech-data-engineer")
    spider.crawler = SimpleNamespace(stats=_Stats())
    return spider


def _html_response(name, *, url=LISTING_URL, status=200):
    return HtmlResponse(url=url, status=status, body=(FIXTURES_DIR / name).read_bytes(), encoding="utf-8")


def _xhr_response(name, *, url=XHR_URL, status=200):
    return TextResponse(url=url, status=status, body=(FIXTURES_DIR / name).read_bytes(), encoding="utf-8")


def _detail_requests(outputs):
    return [request for request in outputs if isinstance(request, Request) and not isinstance(request, FormRequest)]


def _xhr_request(outputs):
    return next(request for request in outputs if isinstance(request, FormRequest))


def test_listing_is_plain_http_scoped_to_the_main_container_and_uses_the_live_fields():
    spider = _spider()
    start_request = next(spider.start_requests())
    outputs = list(spider.parse_listing(_html_response("listing_data_engineer.html")))
    details = _detail_requests(outputs)
    xhr_request = _xhr_request(outputs)

    assert spider.allowed_domains == ["deftech.dou.ua", "jobs.dou.ua"]
    assert spider.custom_settings["DOWNLOAD_HANDLERS"]["https"].endswith("HTTP11DownloadHandler")
    assert "playwright" not in start_request.meta
    assert len(details) == 7
    assert details[0].url == DETAIL_URL.rstrip("/")
    assert details[0].cb_kwargs["listing_item"]["title"] == "Senior Data Engineer"
    assert details[0].cb_kwargs["listing_item"]["company"] == "Acme"
    assert details[0].cb_kwargs["listing_item"]["listing_date_text"] == "30 жовтня"
    assert details[0].cb_kwargs["listing_item"]["listing_salary_text"] == "$2000–3000"
    assert details[0].cb_kwargs["listing_item"]["listing_locations_text"] == "віддалено"
    assert details[0].cb_kwargs["listing_item"]["listing_description"] == "Preview 1"
    assert all("playwright" not in request.meta for request in [*details, xhr_request])
    assert xhr_request.url == XHR_URL
    assert parse_qs(xhr_request.body.decode()) == {"csrfmiddlewaretoken": ["fixture-deftech-csrf"], "count": ["7"]}
    assert xhr_request.headers[b"X-Requested-With"] == b"XMLHttpRequest"
    assert spider.crawler.stats.values["deftech/listing_job_cards"] == 7
    assert spider.crawler.stats.values["deftech/detail_requests"] == 7


def test_xhr_processes_actual_fragment_cards_then_accepts_the_observed_terminal_positive_num_shape():
    spider = _spider()
    initial_outputs = list(spider.parse_listing(_html_response("listing_data_engineer.html")))
    initial_xhr = _xhr_request(initial_outputs)

    nonterminal_outputs = list(
        spider.parse_xhr(
            _xhr_response("xhr_nonterminal.json", url=initial_xhr.url),
            csrf_token="fixture-deftech-csrf",
            count=7,
        )
    )
    terminal_request = _xhr_request(nonterminal_outputs)
    terminal_outputs = list(
        spider.parse_xhr(
            _xhr_response("xhr_terminal.json", url=terminal_request.url),
            csrf_token="fixture-deftech-csrf",
            count=8,
        )
    )

    assert len(_detail_requests(nonterminal_outputs)) == 1
    assert terminal_outputs == []
    assert parse_qs(terminal_request.body.decode())["count"] == ["8"]
    assert spider.crawler.stats.values["deftech/xhr_batches"] == 2
    assert spider.crawler.stats.values["deftech/listing_job_cards"] == 8
    assert spider.crawler.stats.values["deftech/xhr_terminal_empty"] is True
    assert spider.crawler.stats.values["deftech/listing_completed"] is True
    assert spider.crawler.stats.values["deftech/xhr_completed"] is True


def test_valid_zero_listing_requires_its_terminal_xhr_contract():
    spider = _spider()
    outputs = list(spider.parse_listing(_html_response("listing_empty.html")))
    xhr_request = _xhr_request(outputs)
    final_outputs = list(
        spider.parse_xhr(
            TextResponse(
                url=xhr_request.url,
                body=json.dumps({"html": "", "num": 0, "last": True}).encode(),
                encoding="utf-8",
            ),
            csrf_token="fixture-deftech-csrf",
            count=0,
        )
    )

    assert _detail_requests(outputs) == []
    assert final_outputs == []
    assert spider.crawler.stats.values["deftech/listing_completed"] is True


@pytest.mark.parametrize(
    ("response", "reason"),
    (
        (HtmlResponse(url=LISTING_URL, body=b"<html><body>challenge</body></html>", encoding="utf-8"), "listing_contract_invalid"),
        (TextResponse(url=XHR_URL, body=b"not json", encoding="utf-8"), "xhr_malformed_json"),
        (TextResponse(url=XHR_URL, body=json.dumps({"html": "", "num": 0, "last": False}).encode(), encoding="utf-8"), "xhr_non_progress"),
    ),
)
def test_invalid_listing_or_xhr_contracts_fail_closed(response, reason):
    spider = _spider()

    with pytest.raises(CloseSpider):
        if isinstance(response, HtmlResponse):
            list(spider.parse_listing(response))
        else:
            list(spider.parse_xhr(response, csrf_token="fixture-deftech-csrf", count=7))

    assert spider.crawler.stats.values["deftech/infrastructure_failure"] == reason


def test_detail_uses_jobs_dou_canonical_url_and_retains_deftech_evidence_without_affecting_identity():
    spider = _spider()
    listing_outputs = list(spider.parse_listing(_html_response("listing_data_engineer.html")))
    listing_item = _detail_requests(listing_outputs)[0].cb_kwargs["listing_item"]
    item = list(spider.parse_detail(_html_response("detail_374909.html", url=DETAIL_URL), listing_item=listing_item))[0]
    vacancy = item["canonical_vacancy"]

    assert item["canonical_snapshot_complete"] is True
    assert vacancy.source == "dou"
    assert vacancy.source_vacancy_id == "374909"
    assert vacancy.url == "https://jobs.dou.ua/companies/acme/vacancies/374909"
    assert vacancy.published_at.isoformat() == "2026-10-30"
    assert vacancy.raw_source_metadata["source"] == {
        "detail_url": "https://jobs.dou.ua/companies/acme/vacancies/374909",
        "discovery_surface": "deftech",
        "surface_url": "https://deftech.dou.ua/jobs/companies/acme/vacancies/374909",
        "profile": "deftech-data-engineer",
    }


def test_stale_deftech_slug_uses_one_regular_dou_recovery_request_but_keeps_deftech_provenance():
    spider = _spider()
    listing_outputs = list(spider.parse_listing(_html_response("listing_data_engineer.html")))
    listing_item = _detail_requests(listing_outputs)[0].cb_kwargs["listing_item"]
    failure = SimpleNamespace(
        value=SimpleNamespace(response=SimpleNamespace(status=404)),
        request=SimpleNamespace(url=DETAIL_URL, cb_kwargs={"listing_item": listing_item}),
    )

    fallback = spider.errback_detail(failure)
    item = list(
        spider.parse_detail(
            _html_response(
                "detail_374909.html",
                url="https://jobs.dou.ua/companies/acme/vacancies/374909/",
            ),
            listing_item=listing_item,
        )
    )[0]

    assert fallback.url == "https://jobs.dou.ua/companies/acme/vacancies/374909"
    assert fallback.cb_kwargs["listing_item"] is listing_item
    assert item["canonical_vacancy"].raw_source_metadata["source"]["surface_url"] == DETAIL_URL.rstrip("/")


@pytest.mark.parametrize(
    ("replacement", "reason"),
    (
        ("", "detail_canonical_invalid"),
        ('<link rel="canonical" href="https://jobs.dou.ua/companies/acme/vacancies/374909/"><link rel="canonical" href="https://jobs.dou.ua/companies/acme/vacancies/374909/">', "detail_canonical_invalid"),
        ('<link rel="canonical" href="https://deftech.dou.ua/jobs/companies/acme/vacancies/374909/">', "detail_canonical_invalid"),
        ('<link rel="canonical" href="https://jobs.dou.ua/companies/acme/vacancies/999999/">', "detail_canonical_invalid"),
    ),
)
def test_missing_ambiguous_or_mismatched_canonical_link_fails_before_persistence(replacement, reason):
    spider = _spider()
    listing_outputs = list(spider.parse_listing(_html_response("listing_data_engineer.html")))
    listing_item = _detail_requests(listing_outputs)[0].cb_kwargs["listing_item"]
    body = (FIXTURES_DIR / "detail_374909.html").read_text().replace(
        '<link rel="canonical" href="https://jobs.dou.ua/companies/acme/vacancies/374909/?from=canonical#fragment">',
        replacement,
    )
    response = HtmlResponse(url=DETAIL_URL, body=body.encode(), encoding="utf-8")

    with pytest.raises(CloseSpider):
        list(spider.parse_detail(response, listing_item=listing_item))

    assert spider.crawler.stats.values["deftech/infrastructure_failure"] == reason

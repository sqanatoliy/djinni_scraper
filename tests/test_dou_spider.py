import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from scrapy import FormRequest, Request
from scrapy.exceptions import CloseSpider
from scrapy.http import HtmlResponse, TextResponse

from djinni_scraper.spiders.dou import DouSpider


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "dou"
LISTING_URL = "https://jobs.dou.ua/vacancies/?category=Data+Engineer&exp=0-1"
XHR_URL = "https://jobs.dou.ua/vacancies/xhr-load/?category=Data%20Engineer&exp=0-1"
DETAIL_URL = "https://jobs.dou.ua/companies/acme/vacancies/100001/"


class _Stats:
    def __init__(self):
        self.values = {}

    def get_value(self, key, default=None):
        return self.values.get(key, default)

    def set_value(self, key, value):
        self.values[key] = value


def _spider(profile="dou-data-engineer-0-1"):
    spider = DouSpider(profile=profile)
    spider.crawler = SimpleNamespace(stats=_Stats())
    return spider


def _html_response(name, *, url=LISTING_URL, status=200):
    return HtmlResponse(
        url=url,
        status=status,
        body=(FIXTURES_DIR / name).read_bytes(),
        encoding="utf-8",
    )


def _xhr_response(name, *, url=XHR_URL, status=200):
    return TextResponse(
        url=url,
        status=status,
        body=(FIXTURES_DIR / name).read_bytes(),
        encoding="utf-8",
    )


def _detail_requests(outputs):
    return [request for request in outputs if isinstance(request, Request) and not isinstance(request, FormRequest)]


def _xhr_request(outputs):
    return next(request for request in outputs if isinstance(request, FormRequest))


def test_start_request_and_listing_cards_are_plain_http_and_scoped_to_the_vacancy_container():
    spider = _spider()
    start_request = next(spider.start_requests())
    outputs = list(spider.parse_listing(_html_response("listing_full.html")))
    details = _detail_requests(outputs)
    xhr_request = _xhr_request(outputs)

    assert start_request.url == LISTING_URL
    assert spider.custom_settings["DOWNLOAD_HANDLERS"]["https"].endswith("HTTP11DownloadHandler")
    assert "playwright" not in start_request.meta
    assert len(details) == 20
    assert details[0].url == "https://jobs.dou.ua/companies/acme/vacancies/100001"
    assert details[0].cb_kwargs["listing_item"]["title"] == "Junior Data Engineer"
    assert details[0].cb_kwargs["listing_item"]["company"] == "Acme"
    assert details[0].cb_kwargs["listing_item"]["listing_date_text"] == "13 жовтня 2026"
    assert details[0].cb_kwargs["listing_item"]["listing_salary_text"] == "$1000–2000"
    assert details[0].cb_kwargs["listing_item"]["listing_locations_text"] == "Київ, віддалено"
    assert details[0].cb_kwargs["listing_item"]["listing_description"] == "Initial preview 1"
    assert all("playwright" not in request.meta for request in [*details, xhr_request])
    assert xhr_request.url == XHR_URL
    assert parse_qs(xhr_request.body.decode()) == {"csrfmiddlewaretoken": ["fixture-csrf-token"], "count": ["20"]}
    assert xhr_request.headers[b"X-Requested-With"] == b"XMLHttpRequest"
    assert spider.crawler.stats.values["dou/listing_job_cards"] == 20
    assert spider.crawler.stats.values["dou/detail_requests"] == 20


def test_xhr_pagination_preserves_dynamic_filters_and_processes_20_then_7_cards():
    spider = _spider()
    initial_outputs = list(spider.parse_listing(_html_response("listing_full.html")))
    first_request = _xhr_request(initial_outputs)

    first_outputs = list(
        spider.parse_xhr(
            _xhr_response("xhr_page_20.json", url=first_request.url),
            csrf_token="fixture-csrf-token",
            count=20,
        )
    )
    second_request = _xhr_request(first_outputs)
    second_outputs = list(
        spider.parse_xhr(
            _xhr_response("xhr_page_7.json", url=second_request.url),
            csrf_token="fixture-csrf-token",
            count=40,
        )
    )

    assert len(_detail_requests(initial_outputs)) == 20
    assert len(_detail_requests(first_outputs)) == 20
    assert len(_detail_requests(second_outputs)) == 7
    assert parse_qs(second_request.body.decode())["count"] == ["40"]
    assert dict(parse_qs(urlsplit(second_request.url).query)) == {
        "category": ["Data Engineer"],
        "exp": ["0-1"],
    }
    assert spider.crawler.stats.values["dou/xhr_batches"] == 2
    assert spider.crawler.stats.values["dou/xhr_count"] == 47
    assert spider.crawler.stats.values["dou/listing_job_cards"] == 47
    assert spider.crawler.stats.values["dou/listing_completed"] is True
    assert spider.crawler.stats.values["dou/xhr_completed"] is True


def test_valid_empty_listing_uses_explicit_final_xhr_response_and_completes_successfully():
    spider = _spider()
    initial_outputs = list(spider.parse_listing(_html_response("listing_empty.html")))
    xhr_request = _xhr_request(initial_outputs)
    final_outputs = list(
        spider.parse_xhr(
            _xhr_response("xhr_empty_final.json", url=xhr_request.url),
            csrf_token="fixture-csrf-token",
            count=0,
        )
    )

    assert _detail_requests(initial_outputs) == []
    assert final_outputs == []
    assert spider.crawler.stats.values["dou/listing_job_cards"] == 0
    assert spider.crawler.stats.values["dou/listing_completed"] is True


def test_terminal_empty_xhr_batch_is_accepted_only_after_a_nonempty_listing():
    spider = _spider()
    outputs = list(
        spider.parse_xhr(
            TextResponse(
                url=XHR_URL,
                body=json.dumps({"html": "", "num": 40, "last": True}).encode(),
                encoding="utf-8",
            ),
            csrf_token="fixture-csrf-token",
            count=3,
        )
    )

    assert outputs == []
    assert spider.crawler.stats.values["dou/xhr_terminal_empty"] is True
    assert spider.crawler.stats.values["dou/listing_completed"] is True


def test_terminal_positive_empty_xhr_cannot_turn_an_empty_initial_listing_into_success():
    spider = _spider()
    response = TextResponse(
        url=XHR_URL,
        body=json.dumps({"html": "", "num": 40, "last": True}).encode(),
        encoding="utf-8",
    )

    with pytest.raises(CloseSpider):
        list(spider.parse_xhr(response, csrf_token="fixture-csrf-token", count=0))

    assert spider.crawler.stats.values["dou/infrastructure_failure"] == "xhr_initial_listing_mismatch"


def test_terminal_xhr_cards_are_processed_even_when_server_num_is_zero():
    spider = _spider()
    payload = json.loads((FIXTURES_DIR / "xhr_page_7.json").read_text())
    payload["num"] = 0
    response = TextResponse(url=XHR_URL, body=json.dumps(payload).encode(), encoding="utf-8")

    outputs = list(spider.parse_xhr(response, csrf_token="fixture-csrf-token", count=20))

    assert len(_detail_requests(outputs)) == 7
    assert spider.crawler.stats.values["dou/listing_completed"] is True


def test_first_job_profile_uses_its_public_terminal_listing_contract_without_xhr():
    spider = _spider("dou-first-job-remote")
    response = _html_response(
        "first_job_listing_full.html",
        url="https://jobs.dou.ua/first-job/?city=remote",
    )
    outputs = list(spider.parse_listing(response))
    details = _detail_requests(outputs)

    assert len(details) == 2
    assert all(not isinstance(output, FormRequest) for output in outputs)
    assert details[0].url == "https://jobs.dou.ua/companies/acme/vacancies/200001"
    assert details[0].cb_kwargs["listing_item"]["listing_description"] == "First-job preview 1"
    assert details[0].cb_kwargs["listing_item"]["listing_category"] is None
    assert spider.crawler.stats.values["dou/listing_job_cards"] == 2
    assert spider.crawler.stats.values["dou/listing_completed"] is True
    assert spider.crawler.stats.values["dou/xhr_completed"] is True


def test_missing_listing_container_is_not_silently_treated_as_zero_results():
    spider = _spider()
    response = HtmlResponse(url=LISTING_URL, body=b"<html><body>challenge</body></html>", encoding="utf-8")

    with pytest.raises(CloseSpider):
        list(spider.parse_listing(response))

    assert spider.crawler.stats.values["dou/infrastructure_failure"] == "listing_contract_invalid"


@pytest.mark.parametrize(
    ("payload", "reason"),
    (
        (b"not json", "xhr_malformed_json"),
        (json.dumps({"num": 1, "last": False}).encode(), "xhr_malformed_payload"),
        (json.dumps({"html": "", "last": False}).encode(), "xhr_malformed_payload"),
        (json.dumps({"html": "", "num": 1}).encode(), "xhr_malformed_payload"),
        (json.dumps({"html": "", "num": -1, "last": False}).encode(), "xhr_malformed_payload"),
        (json.dumps({"html": "", "num": 0, "last": False}).encode(), "xhr_non_progress"),
        (json.dumps({"html": "", "num": 1, "last": False}).encode(), "xhr_listing_contract_invalid"),
    ),
)
def test_malformed_or_nonprogress_xhr_responses_fail_explicitly(payload, reason):
    spider = _spider()
    response = TextResponse(url=XHR_URL, body=payload, encoding="utf-8")

    with pytest.raises(CloseSpider):
        list(spider.parse_xhr(response, csrf_token="fixture-csrf-token", count=20))

    assert spider.crawler.stats.values["dou/infrastructure_failure"] == reason


def test_detail_parser_requires_complete_detail_contract_then_yields_canonical_dou_item():
    spider = _spider()
    listing_outputs = list(spider.parse_listing(_html_response("listing_full.html")))
    listing_item = _detail_requests(listing_outputs)[0].cb_kwargs["listing_item"]
    detail_response = _html_response("detail_full.html", url=DETAIL_URL)

    item = list(spider.parse_detail(detail_response, listing_item=listing_item))[0]
    vacancy = item["canonical_vacancy"]

    assert item["canonical_snapshot_complete"] is True
    assert vacancy.source == "dou"
    assert vacancy.source_vacancy_id == "100001"
    assert vacancy.url == DETAIL_URL.rstrip("/")
    assert vacancy.title == "Junior Data Engineer"
    assert vacancy.company.name == "Acme"
    assert vacancy.company.url == "https://jobs.dou.ua/companies/acme/"
    assert vacancy.category == "Data Engineer"
    assert vacancy.experience_months is None
    assert vacancy.raw_source_metadata["detail"]["filter_links"][1]["text"] == "Data Engineer"
    assert spider.crawler.stats.values["dou/detail_completed"] == 1


def test_detail_without_required_description_fails_the_profile_instead_of_persisting_listing_only_data():
    spider = _spider()
    listing_item = list(spider.parse_listing(_html_response("listing_full.html")))[0].cb_kwargs["listing_item"]
    response = HtmlResponse(
        url=DETAIL_URL,
        body=b'<div class="b-vacancy"><h1 class="g-h2">Role</h1></div>',
        encoding="utf-8",
    )

    with pytest.raises(CloseSpider):
        list(spider.parse_detail(response, listing_item=listing_item))

    assert spider.crawler.stats.values["dou/infrastructure_failure"] == "detail_contract_invalid"

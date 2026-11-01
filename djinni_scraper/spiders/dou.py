"""Plain-Scrapy DOU vacancy source with public XHR pagination semantics."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

import scrapy
from parsel import Selector
from scrapy.exceptions import CloseSpider

from djinni_scraper.adapters.dou_vacancy import (
    dou_vacancy_id_from_url,
    normalize_dou_url,
    normalize_dou_vacancy,
)
from djinni_scraper.dou_profiles import DouSearchProfile, get_dou_search_profile
from djinni_scraper.items import DouScraperItem


class DouSelectors:
    """Current public DOU HTML selectors, deliberately scoped to the listing container."""

    LISTING_CONTAINER = "#vacancyListId"
    LISTING_LIST = "#vacancyListId ul.lt"
    LISTING_CARD = "#vacancyListId ul.lt > li.l-vacancy"
    FIRST_JOB_CONTAINER = "div.first-job-vacancies"
    LISTING_DATE = "div.date::text"
    LISTING_TITLE = "div.title > a::text"
    LISTING_URL = "div.title > a::attr(href)"
    LISTING_COMPANY = "div.title > strong a.company::text"
    LISTING_SALARY = "span.salary::text"
    LISTING_LOCATIONS = "span.cities::text"
    LISTING_DESCRIPTION = "div.sh-info::text"
    DETAIL_TITLE = ".b-vacancy h1.g-h2::text"
    DETAIL_COMPANY = ".b-compinfo .l-n > a:first-child::text"
    DETAIL_COMPANY_URL = ".b-compinfo .l-n > a:first-child::attr(href)"
    DETAIL_DATE = ".b-vacancy .l-vacancy > .date::text"
    DETAIL_LOCATIONS = ".b-vacancy .sh-info .place::text"
    DETAIL_SALARY = ".b-vacancy .sh-info .salary::text"
    DETAIL_DESCRIPTION = ".b-vacancy .b-typo.vacancy-section"
    DETAIL_BREADCRUMBS = ".breadcrumbs a"


_CSRF_TOKEN_RE = re.compile(
    r"window\.CSRF_TOKEN\s*=\s*(?P<quote>[\"'])(?P<token>[^\"']+)(?P=quote)"
)
_XHR_URL_RE = re.compile(
    r"window\.XHR_VS_URL\s*=\s*(?P<quote>[\"'])(?P<url>[^\"']+)(?P=quote)"
)


class DouSpider(scrapy.Spider):
    """Scrapes one typed DOU profile through public HTTP only; no browser or auth state."""

    name = "dou"
    allowed_domains = ["jobs.dou.ua"]
    custom_settings = {
        # Project defaults use scrapy-playwright for Djinni. DOU must never
        # initialize a browser merely because both spiders share a project.
        "DOWNLOAD_HANDLERS": {
            "https": "scrapy.core.downloader.handlers.http11.HTTP11DownloadHandler",
        },
    }

    def __init__(self, profile: str, **kwargs: Any):
        super().__init__(**kwargs)
        if kwargs:
            conflicting_args = ", ".join(sorted(kwargs))
            raise ValueError(f"DOU profile mode cannot be combined with ad-hoc arguments: {conflicting_args}")
        self.search_profile: DouSearchProfile = get_dou_search_profile(profile)
        self.discovery_profile = self.search_profile.slug
        self.notification_route = self.search_profile.notification_route
        self.start_urls = [self.search_profile.search_url]

    def start_requests(self) -> Iterable[scrapy.Request]:
        self.logger.info(
            "DOU profile=%s search_url=%s route=%s",
            self.search_profile.slug,
            self.start_urls[0],
            self.notification_route,
        )
        self._set_run_stat("dou/search_requested", True)
        self._set_run_stat("dou/search_url", self.start_urls[0])
        yield scrapy.Request(
            self.start_urls[0],
            callback=self.parse_listing,
            errback=self.errback_listing,
        )

    def parse_listing(self, response: scrapy.http.Response):
        """Parses an initial scoped listing and starts cookie-preserving XHR pagination."""
        if response.status != 200:
            self._fail("listing_http_error", f"DOU listing returned HTTP {response.status}")
        if self.search_profile.query.first_job:
            cards = self._first_job_cards_or_fail(response)
            self._record_listing_page(len(cards))
            self.logger.info("dou-first-job-listing cards=%s", len(cards))
            for card in cards:
                yield self._detail_request(self._parse_listing_card(card, response))
            # The public First Job page has no XHR endpoint. Its initial
            # response is therefore terminal for this profile.
            self._set_run_stat("dou/xhr_completed", True)
            self._set_run_stat("dou/listing_completed", True)
            return
        cards = self._listing_cards_or_fail(response.css(DouSelectors.LISTING_CONTAINER))
        self._record_listing_page(len(cards))
        self.logger.info("dou-listing-page cards=%s", len(cards))
        for card in cards:
            yield self._detail_request(self._parse_listing_card(card, response))

        xhr_url = self._xhr_url_or_fail(response)
        csrf_token = self._csrf_token_or_fail(response)
        yield self._xhr_request(xhr_url=xhr_url, csrf_token=csrf_token, count=len(cards))

    def parse_xhr(self, response: scrapy.http.Response, *, csrf_token: str, count: int):
        """Processes one XHR batch and terminates only when DOU marks it final."""
        if response.status != 200:
            self._fail("xhr_http_error", f"DOU XHR returned HTTP {response.status}")
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as error:
            self._fail("xhr_malformed_json", f"DOU XHR returned invalid JSON: {type(error).__name__}")
        if not isinstance(payload, dict):
            self._fail("xhr_malformed_payload", "DOU XHR JSON must be an object")

        html = payload.get("html")
        number = payload.get("num")
        last = payload.get("last")
        if not isinstance(html, str) or isinstance(number, bool) or not isinstance(number, int) or number < 0:
            self._fail("xhr_malformed_payload", "DOU XHR must contain html text and a non-negative num")
        if not isinstance(last, bool):
            self._fail("xhr_malformed_payload", "DOU XHR must contain a boolean last")

        next_count = count + number
        self._increment_run_stat("dou/xhr_batches")
        self._set_run_stat("dou/xhr_count", next_count)
        self.logger.info("dou-xhr-page num=%s last=%s count=%s", number, last, next_count)

        if not last and number <= 0:
            self._fail("xhr_non_progress", "DOU XHR reported no progress before its final batch")

        fragment = Selector(text=html, type="html")
        cards = fragment.css("li.l-vacancy")
        if cards:
            self._record_listing_cards(len(cards))
            for card in cards:
                yield self._detail_request(self._parse_listing_card(card, response))
        elif number > 0:
            # DOU's current public endpoint may explicitly signal terminal
            # completion with nonzero ``num`` and empty HTML after a nonempty
            # initial page. Treat only that observed shape as final; any other
            # empty positive batch is a contract failure, never a zero result.
            if not last:
                self._fail("xhr_listing_contract_invalid", "DOU XHR num was positive but contained no vacancy cards")
            if count <= 0:
                self._fail(
                    "xhr_initial_listing_mismatch",
                    "DOU XHR reported terminal progress after an empty initial listing",
                )
            self._set_run_stat("dou/xhr_terminal_empty", True)
            self.logger.info("dou-xhr-terminal-empty num=%s count=%s", number, next_count)

        if last:
            self._set_run_stat("dou/xhr_completed", True)
            self._set_run_stat("dou/listing_completed", True)
            return
        yield self._xhr_request(xhr_url=response.url, csrf_token=csrf_token, count=next_count)

    def parse_detail(self, response: scrapy.http.Response, *, listing_item: DouScraperItem):
        """Requires DOU detail title, description, and numeric identity before persistence."""
        if response.status != 200:
            self._fail("detail_http_error", f"DOU detail returned HTTP {response.status}")
        canonical_url = normalize_dou_url(response.url)
        if dou_vacancy_id_from_url(canonical_url) is None:
            self._fail("detail_identity_missing", "DOU detail URL has no numeric vacancy identity")
        title = _text(response.css(DouSelectors.DETAIL_TITLE).get())
        description_html = response.css(DouSelectors.DETAIL_DESCRIPTION).get()
        if title is None or description_html is None:
            self._fail("detail_contract_invalid", "DOU detail requires one title and one description container")

        breadcrumbs = [
            {"text": _text(link.css("::text").get()) or "", "href": response.urljoin(href)}
            for link in response.css(DouSelectors.DETAIL_BREADCRUMBS)
            if (href := link.css("::attr(href)").get())
        ]
        category = breadcrumbs[1]["text"] if len(breadcrumbs) > 1 and breadcrumbs[1]["text"] else None
        enriched_item = DouScraperItem(listing_item)
        enriched_item.update(
            {
                "source_vacancy_id": dou_vacancy_id_from_url(canonical_url),
                "canonical_url": canonical_url,
                "detail_title": title,
                "company": _text(response.css(DouSelectors.DETAIL_COMPANY).get()) or listing_item.get("company"),
                "company_url": _absolute_url(response, response.css(DouSelectors.DETAIL_COMPANY_URL).get()),
                "detail_date_text": _text(response.css(DouSelectors.DETAIL_DATE).get()),
                "detail_salary_text": _text(response.css(DouSelectors.DETAIL_SALARY).get()),
                "detail_locations_text": _text(response.css(DouSelectors.DETAIL_LOCATIONS).get()),
                "detail_description_html": description_html,
                "detail_category": category,
                "detail_filter_links": breadcrumbs,
                "raw_dou_metadata": {"detail_url": canonical_url},
                "canonical_snapshot_complete": True,
            }
        )
        try:
            enriched_item["canonical_vacancy"] = normalize_dou_vacancy(enriched_item)
        except ValueError as error:
            self._fail("detail_normalization_failed", f"DOU detail normalization failed: {error}")
        self._increment_run_stat("dou/detail_completed")
        yield enriched_item

    def errback_listing(self, failure: Any):
        self._fail("listing_request_failed", f"DOU listing request failed: {failure.getErrorMessage()}")

    def errback_xhr(self, failure: Any):
        self._fail("xhr_request_failed", f"DOU XHR request failed: {failure.getErrorMessage()}")

    def errback_detail(self, failure: Any):
        self._fail("detail_request_failed", f"DOU detail request failed: {failure.getErrorMessage()}")

    def _listing_cards_or_fail(self, container: scrapy.selector.unified.SelectorList):
        if len(container) != 1:
            self._fail("listing_contract_invalid", "DOU listing container #vacancyListId is missing or ambiguous")
        listing = container.css("ul.lt")
        if len(listing) != 1:
            self._fail("listing_contract_invalid", "DOU listing container has no unambiguous ul.lt")
        return listing.css("li.l-vacancy")

    def _first_job_cards_or_fail(self, response: scrapy.http.Response):
        container = response.css(DouSelectors.FIRST_JOB_CONTAINER)
        if len(container) != 1:
            self._fail(
                "listing_contract_invalid",
                "DOU first-job listing container .first-job-vacancies is missing or ambiguous",
            )
        listing = container.xpath("./ul")
        if len(listing) != 1:
            self._fail(
                "listing_contract_invalid",
                "DOU first-job listing container has no unambiguous direct vacancy list",
            )
        return listing.xpath(
            "./li[contains(concat(' ', normalize-space(@class), ' '), ' l-vacancy ')]"
        )

    def _xhr_url_or_fail(self, response: scrapy.http.Response) -> str:
        match = _XHR_URL_RE.search(response.text)
        if match is None:
            self._fail("xhr_contract_invalid", "DOU listing does not expose window.XHR_VS_URL")
        return response.urljoin(match.group("url"))

    def _csrf_token_or_fail(self, response: scrapy.http.Response) -> str:
        match = _CSRF_TOKEN_RE.search(response.text)
        if match is None:
            self._fail("csrf_unavailable", "DOU listing does not expose window.CSRF_TOKEN")
        return match.group("token")

    def _xhr_request(self, *, xhr_url: str, csrf_token: str, count: int) -> scrapy.FormRequest:
        return scrapy.FormRequest(
            xhr_url,
            formdata={"csrfmiddlewaretoken": csrf_token, "count": str(count)},
            callback=self.parse_xhr,
            cb_kwargs={"csrf_token": csrf_token, "count": count},
            errback=self.errback_xhr,
            headers={"Referer": self.start_urls[0], "X-Requested-With": "XMLHttpRequest"},
            dont_filter=True,
        )

    def _detail_request(self, listing_item: DouScraperItem) -> scrapy.Request:
        self._increment_run_stat("dou/detail_requests")
        return scrapy.Request(
            listing_item["url"],
            callback=self.parse_detail,
            cb_kwargs={"listing_item": listing_item},
            errback=self.errback_detail,
        )

    def _parse_listing_card(self, card: scrapy.selector.unified.Selector, response: scrapy.http.Response) -> DouScraperItem:
        href = card.css(DouSelectors.LISTING_URL).get()
        url = _absolute_url(response, href)
        if url is None or dou_vacancy_id_from_url(url) is None:
            self._fail("listing_identity_missing", "DOU listing card has no numeric vacancy URL")
        return DouScraperItem(
            title=_text(card.css(DouSelectors.LISTING_TITLE).get()),
            url=normalize_dou_url(url),
            source_vacancy_id=dou_vacancy_id_from_url(url),
            company=_text(card.css(DouSelectors.LISTING_COMPANY).get()),
            listing_date_text=_text(card.css(DouSelectors.LISTING_DATE).get()),
            listing_salary_text=_text(card.css(DouSelectors.LISTING_SALARY).get()),
            listing_locations_text=_text(card.css(DouSelectors.LISTING_LOCATIONS).get()),
            listing_description=_text(card.css(DouSelectors.LISTING_DESCRIPTION).get()),
            listing_category=self.search_profile.query.category,
            listing_profile=self.discovery_profile,
        )

    def _record_listing_page(self, card_count: int) -> None:
        self._increment_run_stat("dou/listing_pages")
        self._record_listing_cards(card_count)

    def _record_listing_cards(self, card_count: int) -> None:
        self._set_run_stat(
            "dou/listing_job_cards", self._run_stat_value("dou/listing_job_cards") + card_count
        )

    def _increment_run_stat(self, key: str) -> None:
        self._set_run_stat(key, self._run_stat_value(key) + 1)

    def _set_run_stat(self, key: str, value: object) -> None:
        crawler = getattr(self, "crawler", None)
        if crawler is not None:
            crawler.stats.set_value(key, value)

    def _run_stat_value(self, key: str) -> int:
        crawler = getattr(self, "crawler", None)
        return int(crawler.stats.get_value(key, 0)) if crawler is not None else 0

    def _fail(self, reason: str, message: str) -> None:
        self._set_run_stat("dou/infrastructure_failure", reason)
        self.logger.error("DOU failure profile=%s stage=%s: %s", self.discovery_profile, reason, message)
        raise CloseSpider(message)


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _absolute_url(response: scrapy.http.Response, value: object) -> str | None:
    return response.urljoin(value) if isinstance(value, str) and value.strip() else None

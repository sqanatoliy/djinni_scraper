import os
from urllib.parse import urljoin as urllib_urljoin, urlparse

import scrapy
from decouple import config
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from scrapy import signals
from scrapy.exceptions import CloseSpider

from djinni_scraper.adapters.djinni_vacancy import normalize_djinni_vacancy
from djinni_scraper.parsers.djinni_jsonld import parse_detail_metadata
from djinni_scraper.utils.url_utils import BASE_URL, get_start_url
from ..items import DjinniScraperItem

STATE_FILE = "state.json"
LOGIN_URL = urllib_urljoin(BASE_URL, "/login?lang=uk&from=jobs&next=/jobs/")
LOGIN_TIMEOUT_MS = 10_000


class DjinniSelectors:
    LOGIN_FORM_SELECTOR = "form#signup"
    EMAIL_SELECTOR = "#email"
    PASSWORD_SELECTOR = "#password"
    LOGIN_BUTTON_SELECTOR = 'form#signup button[type="submit"]:not([name])'
    AUTHENTICATED_MARKER_SELECTOR = 'a[href^="/logout"]'
    JOBS_LIST_SELECTOR = ".list-jobs .job-item"
    JOB_LINK_SELECTOR = ".job_item__header-link"
    JOB_TITLE_SELECTOR = ".job_item__header-link .job-item__position"
    JOB_COMPANY_SELECTOR = ".job_item__header-link .small.text-gray-800.opacity-75.font-weight-500"
    JOB_SALARY_SELECTOR = ".job_item__header-link .text-success.text-nowrap"
    JOB_DESCRIPTION_SELECTOR = ".js-truncated-text"
    JOB_TAGS_SELECTOR = ".job-item__tags .badge"
    JOB_PUBLICATION_DATE_SELECTOR = "span.text-nowrap[data-bs-original-title]"
    PAGINATION_ITEMS_SELECTOR = "ul.pagination_with_numbers li.page-item"


class DjinniSpider(scrapy.Spider):
    name = "djinni"
    allowed_domains = ["djinni.co"]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.params = {}
        for key, value in kwargs.items():
            if "," in value:
                self.params[key] = value.split(",")
            else:
                self.params[key] = value

        self.start_urls = [get_start_url(self.params)]
        self.category = self.params.get("primary_keyword", "all")

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        """Реєструємо `open_spider, close_spider`"""
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider.open_spider, signal=signals.spider_opened)
        crawler.signals.connect(spider.close_spider, signal=signals.spider_closed)
        return spider

    def open_spider(self, spider):
        """Відкриваємо Spider"""
        spider.logger.info("✅Spider opened: %s", spider.name)

    def close_spider(self, spider):
        """Закриваємо Spider"""
        spider.logger.info("✅Spider closed: %s", spider.name)

    def start_requests(self):
        """Reuses a local session or starts one explicit login attempt."""
        if self._storage_state_exists():
            self.logger.info(f"✅ Знайдено state.json! Використовуємо збережену сесію. URL: {self.start_urls[0]}")
            yield self._jobs_request(self.start_urls[0])
        else:
            self.logger.info("🔄 Виконуємо авторизацію...")
            yield self._login_request(self.start_urls[0], auth_recovery_attempted=False)

    async def login(self, response):
        """Logs in and resumes the one jobs URL that initiated this auth flow."""
        page = response.meta.get("playwright_page")
        target_url = response.meta.get("auth_target_url", self.start_urls[0])
        try:
            if not page:
                raise CloseSpider("Djinni login did not receive a Playwright page")

            email, password = self._get_credentials()
            await page.wait_for_selector(DjinniSelectors.LOGIN_FORM_SELECTOR, timeout=LOGIN_TIMEOUT_MS)
            await page.fill(DjinniSelectors.EMAIL_SELECTOR, email)
            await page.fill(DjinniSelectors.PASSWORD_SELECTOR, password)
            await page.click(DjinniSelectors.LOGIN_BUTTON_SELECTOR)

            if not await self._wait_for_authenticated(page):
                self._discard_storage_state()
                raise CloseSpider("Djinni login was rejected or remained anonymous")

            try:
                await page.context.storage_state(path=STATE_FILE)
            except PlaywrightError:
                self.logger.exception("Failed to save confirmed Djinni Playwright storage state.")
                raise

            self.logger.info("✅ Djinni login succeeded; local storage state saved.")
            yield self._jobs_request(
                target_url,
                auth_recovery_attempted=response.meta.get("auth_recovery_attempted", False),
            )
        except PlaywrightTimeoutError as error:
            self._discard_storage_state()
            self.logger.error("Djinni login form or authenticated marker was not available before timeout.")
            raise CloseSpider("Djinni login timed out or remained anonymous") from error
        except PlaywrightError as error:
            self._discard_storage_state()
            self.logger.error("Djinni Playwright error during login: %s", error)
            raise CloseSpider("Djinni login failed due to a Playwright error") from error
        finally:
            await self._close_page(page)

    async def parse_jobs(self, response):
        """Парсимо вакансії після авторизації."""
        page = response.meta.get("playwright_page")
        if not page:
            raise CloseSpider("Djinni jobs request did not receive a Playwright page")

        try:
            if not await self._is_authenticated(page):
                self._discard_storage_state()
                if response.meta.get("auth_recovery_attempted", False):
                    raise CloseSpider("Djinni authentication recovery failed; anonymous jobs page was not parsed")

                target_url = response.meta.get("auth_target_url", response.url)
                self.logger.warning("Djinni session is invalid; starting one login recovery attempt.")
                yield self._login_request(target_url, auth_recovery_attempted=True)
                return

            self.logger.info("✅ Авторизація активна.")
            for job in response.css(DjinniSelectors.JOBS_LIST_SELECTOR):
                yield self._detail_request(self._parse_job_card(job, response))
            async for next_page_request in self.parse_pagination(response):
                yield next_page_request
        finally:
            await self._close_page(page)

    def _jobs_request(self, url, auth_recovery_attempted=False):
        return scrapy.Request(
            url=url,
            callback=self.parse_jobs,
            meta={
                "playwright": True,
                "playwright_context": "default",
                "playwright_include_page": True,
                "auth_target_url": url,
                "auth_recovery_attempted": auth_recovery_attempted,
            },
            errback=self.errback_close_page,
        )

    def _login_request(self, target_url, auth_recovery_attempted):
        self._get_credentials()
        return scrapy.Request(
            url=LOGIN_URL,
            callback=self.login,
            meta={
                "playwright": True,
                "playwright_context": "default",
                "playwright_include_page": True,
                "auth_target_url": target_url,
                "auth_recovery_attempted": auth_recovery_attempted,
            },
            errback=self.errback_close_page,
            dont_filter=True,
        )

    @staticmethod
    def _storage_state_exists():
        return os.path.exists(STATE_FILE)

    def _discard_storage_state(self):
        if self._storage_state_exists():
            os.remove(STATE_FILE)
            self.logger.info("Removed stale local Djinni Playwright storage state.")

    @staticmethod
    def _get_credentials():
        credentials = {
            "DJINNI_EMAIL": config("DJINNI_EMAIL", default="").strip(),
            "DJINNI_PASSWORD": config("DJINNI_PASSWORD", default="").strip(),
        }
        missing = [name for name, value in credentials.items() if not value]
        if missing:
            raise CloseSpider(f"Missing required Djinni configuration: {', '.join(missing)}")
        return credentials["DJINNI_EMAIL"], credentials["DJINNI_PASSWORD"]

    async def _wait_for_authenticated(self, page):
        try:
            await page.wait_for_selector(
                DjinniSelectors.AUTHENTICATED_MARKER_SELECTOR,
                timeout=LOGIN_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            return False
        return await self._is_authenticated(page)

    @staticmethod
    async def _is_authenticated(page):
        current_path = urlparse(page.url).path.rstrip("/")
        if current_path == "/login":
            return False

        login_form_count = await page.locator(DjinniSelectors.LOGIN_FORM_SELECTOR).count()
        authenticated_marker_count = await page.locator(
            DjinniSelectors.AUTHENTICATED_MARKER_SELECTOR
        ).count()
        return login_form_count == 0 and authenticated_marker_count > 0

    async def _close_page(self, page):
        if page:
            try:
                await page.close()
            except PlaywrightError as error:
                self.logger.warning("Unable to close Djinni Playwright page cleanly: %s", error)

    def _detail_request(self, listing_item):
        """Requests public vacancy details without Playwright and retains listing context."""
        return scrapy.Request(
            url=listing_item["url"],
            callback=self.parse_job_detail,
            cb_kwargs={"listing_item": listing_item},
            errback=self.errback_detail_request,
        )

    def parse_job_detail(self, response, listing_item):
        """Enriches one listing item with optional public Djinni detail metadata."""
        detail_metadata = parse_detail_metadata(response, logger=self.logger)
        enriched_item = DjinniScraperItem(listing_item)
        enriched_item.update(detail_metadata)

        self._log_detail_mismatches(listing_item, detail_metadata)
        try:
            enriched_item["canonical_vacancy"] = normalize_djinni_vacancy(
                enriched_item,
                logger=self.logger,
            )
        except ValueError as error:
            self.logger.error("Djinni canonical vacancy normalization failed for %s: %s", response.url, error)
        yield enriched_item

    def errback_detail_request(self, failure):
        self.logger.error("Djinni vacancy detail request failed: %s", failure.getErrorMessage())

    def _log_detail_mismatches(self, listing_item, detail_metadata):
        detail_title = detail_metadata.get("detail_title")
        if detail_title and detail_title != listing_item.get("title"):
            self.logger.debug(
                "Djinni title differs between listing and detail for %s: %r != %r",
                listing_item.get("url"),
                listing_item.get("title"),
                detail_title,
            )

        canonical_url = detail_metadata.get("canonical_url")
        if canonical_url and canonical_url != listing_item.get("url", "").rstrip("/"):
            self.logger.debug(
                "Djinni canonical URL differs from listing URL: %s != %s",
                listing_item.get("url"),
                canonical_url,
            )

    def _parse_job_card(self, job, response) -> DjinniScraperItem:
        """Extracts the current public Djinni listing-card fields."""
        href = job.css(f"{DjinniSelectors.JOB_LINK_SELECTOR}::attr(href)").get()
        tags = [
            self._normalize_text(tag, default="")
            for tag in job.css(f"{DjinniSelectors.JOB_TAGS_SELECTOR}::text").getall()
        ]

        return DjinniScraperItem(
            title=self._normalize_text(job.css(f"{DjinniSelectors.JOB_TITLE_SELECTOR}::text").get()),
            salary=self._normalize_text(job.css(f"{DjinniSelectors.JOB_SALARY_SELECTOR}::text").get()),
            company=self._normalize_text(job.css(f"{DjinniSelectors.JOB_COMPANY_SELECTOR}::text").get()),
            views="Not specified",
            responses="Not specified",
            pub_date=self._normalize_text(
                job.css(f"{DjinniSelectors.JOB_PUBLICATION_DATE_SELECTOR}::attr(data-bs-original-title)").get()
            ),
            truncate_description=self._normalize_text(
                job.css(f"{DjinniSelectors.JOB_DESCRIPTION_SELECTOR}::text").get()
            ),
            tags=[tag for tag in tags if tag],
            url=response.urljoin(href) if href else "Not specified",
            category=self.category,
        )

    @staticmethod
    def _normalize_text(value, default="Not specified") -> str:
        normalized = " ".join(value.split()) if value else ""
        return normalized or default

    async def parse_pagination(self, response):
        """Обробляє пагінацію та переходить на наступну сторінку."""
        next_page_url = self._get_next_page_url(response)
        if not next_page_url:
            self.logger.info("🛑 Наступної сторінки немає. Завершуємо парсинг.")
            return

        next_page_element = response.css("li.page-item.active + li.page-item a.page-link")
        next_page_value = next_page_element.css("::text").get(default="").strip()
        if next_page_value.isdigit():
            self.logger.info(f"➡ Переходимо на сторінку {next_page_value}: {next_page_url}")

            yield scrapy.Request(
                next_page_url,
                callback=self.parse_jobs,
                meta={
                    "playwright": True,
                    "playwright_context": "default",
                    "playwright_include_page": True,
                    "auth_target_url": next_page_url,
                    "auth_recovery_attempted": False,
                },
                dont_filter=True,  # ✅ Дозволяє Scrapy робити повторні запити
                errback=self.errback_close_page,
            )
        else:
            self.logger.warning("🚨 `parse_pagination()` не знайшла наступної сторінки!")

    @staticmethod
    def _get_next_page_url(response):
        next_page_link = response.css(
            "li.page-item.active + li.page-item:not(.disabled) a.page-link::attr(href)"
        ).get()
        return urllib_urljoin(response.url, next_page_link) if next_page_link else None

    async def errback_close_page(self, failure):
        """Закриває Playwright сторінку у випадку помилки."""
        page: Page = failure.request.meta.get("playwright_page")
        if page:
            await page.close()
            self.logger.warning("❌ Сторінка Playwright закрита через помилку.")
        else:
            self.logger.error("🚨 Помилка: не вдалося отримати `playwright_page` у `errback`.")

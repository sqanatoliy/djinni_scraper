import os
from urllib.parse import urljoin as urllib_urljoin

import scrapy
from decouple import config
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from scrapy import signals

from djinni_scraper.utils.url_utils import get_start_url
from ..items import DjinniScraperItem

DJINNI_EMAIL = config("DJINNI_EMAIL")
DJINNI_PASSWORD = config("DJINNI_PASSWORD")


class DjinniSelectors:
    LOGIN_SELECTOR = "ul > li:nth-child(1) > a.jobs-push-login-link"
    EMAIL_SELECTOR = "#email"
    PASSWORD_SELECTOR = "#password"
    LOGIN_BUTTON_SELECTOR = 'button.btn-primary[type="submit"]'
    PROFILE_SELECTOR = "div.navbar-collapse div.user-name"
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
        """Перевіряємо, чи є збережений стан. Якщо є — використовуємо, інакше логінимось."""
        if os.path.exists("state.json"):
            self.logger.info(f"✅ Знайдено state.json! Використовуємо збережену сесію. URL: {self.start_urls[0]}")
            yield scrapy.Request(
                url=self.start_urls[0],
                callback=self.parse_jobs,
                meta={
                    "playwright": True,
                    "playwright_context": "default",
                    "playwright_include_page": True,
                },
                errback=self.errback_close_page,
            )
        else:
            self.logger.info("🔄 Виконуємо авторизацію...")
            yield scrapy.Request(
                url="https://djinni.co/",
                callback=self.login,
                meta={
                    "playwright": True,
                    "playwright_context": "default",
                    "playwright_include_page": True,
                },
                errback=self.errback_close_page,
            )

    @staticmethod
    async def log_request(route, request):
        print(f"📡 Запит: {request.url} | Метод: {request.method}")
        await route.continue_()

    async def login(self, response):
        """Autologin to Djinni.co."""
        page = response.meta["playwright_page"]

        await page.route("**", self.log_request)
        await page.wait_for_selector(DjinniSelectors.LOGIN_SELECTOR)
        await page.click(DjinniSelectors.LOGIN_SELECTOR)

        await page.wait_for_selector(DjinniSelectors.LOGIN_BUTTON_SELECTOR, timeout=10000)
        await page.wait_for_timeout(3000)
        await page.fill(DjinniSelectors.EMAIL_SELECTOR, DJINNI_EMAIL)
        await page.fill(DjinniSelectors.PASSWORD_SELECTOR, DJINNI_PASSWORD)
        await page.click(DjinniSelectors.LOGIN_BUTTON_SELECTOR)

        try:
            await page.wait_for_selector(DjinniSelectors.PROFILE_SELECTOR, timeout=10000)
            self.logger.info("✅ Логін успішний! Зберігаємо state.json.")
            await page.context.storage_state(path="state.json")
        except PlaywrightTimeoutError as err:
            self.logger.error(f"❌ Помилка логіну! {err} Видаляємо state.json.")
            if os.path.exists("state.json"):
                os.remove("state.json")
        finally:
            await page.close()
        yield scrapy.Request(
            url=self.start_urls[0],
            callback=self.parse_jobs,
            meta={
                "playwright": True,
                "playwright_context": "default",
                "playwright_include_page": True,
            },
            errback=self.errback_close_page,
        )

    async def parse_jobs(self, response):
        """Парсимо вакансії після авторизації."""
        page = response.meta["playwright_page"]
        if not page:
            self.logger.error("❌ Не отримано Playwright page. Завантажуємо без нього.")
            return
        try:
            await page.wait_for_selector(DjinniSelectors.PROFILE_SELECTOR, timeout=5000)
            self.logger.info("✅ Авторизація активна.")
        except PlaywrightTimeoutError:
            self.logger.warning("❌ Авторизація не знайдена. Видаляємо state.json.")
            os.remove("state.json")

        for job in response.css(DjinniSelectors.JOBS_LIST_SELECTOR):
            yield self._parse_job_card(job, response)
        self.logger.info("🛑 Закриваємо сторінку Playwright перед переходом на наступну.")
        await page.close()
        async for next_page_request in self.parse_pagination(response):
            yield next_page_request

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

import os
from urllib.parse import urljoin as urllib_urljoin

import scrapy
from decouple import config
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

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
    JOBS_LIST_SELECTOR = "ul.list-jobs > li"
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

    def start_requests(self):
        """Перевіряємо, чи є збережений стан. Якщо є — використовуємо, інакше логінимось."""
        if os.path.exists("state.json"):
            self.logger.info(f"✅ Знайдено state.json! Використовуємо збережену сесію. URL: {self.start_urls[0]}")
            yield scrapy.Request(
                url=self.start_urls[0],
                meta={
                    "playwright": True,
                    "playwright_context": "default",
                },
                callback=self.parse_jobs,
            )
        else:
            self.logger.info("🔄 Виконуємо авторизацію...")
            yield scrapy.Request(
                url="https://djinni.co/",
                meta={
                    "playwright": True,
                    "playwright_context": "default",
                    "playwright_include_page": True,
                },
                callback=self.login,
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
        await page.close()
        yield scrapy.Request(
            url=self.start_urls[0],
            meta={"playwright": True, "playwright_context": "default"},
            callback=self.parse_jobs,
        )

    def parse_jobs(self, response):
        """Парсимо вакансії після авторизації."""
        jobs = response.css(DjinniSelectors.JOBS_LIST_SELECTOR)
        item: DjinniScraperItem = DjinniScraperItem()
        for job in jobs:
            title = job.css("h2 > a::text").get(default="Not specified").strip()
            salary = job.css("h2 > strong > span::text").get(default="Not specified").strip()
            company = job.css("a.text-body.js-analytics-event::text").get(default="Not specified").strip()
            views = job.css("div > div:nth-child(2) > span:nth-child(2)::text").get(default="Not specified").strip()
            responses = job.css("div > div:nth-child(2) > span:nth-child(4)::text").get(default="Not specified").strip()
            pub_date = job.css("div > div:nth-child(2) > span:nth-child(6)::attr(data-original-title)").get(default="Not specified").strip()
            truncate_description = job.css("span.js-truncated-text::text").get(default="Not specified").strip()
            raw_tags = job.css("h2 + div.fw-medium span::text").getall()
            tags = [tag.strip() for tag in raw_tags if tag.strip() and '·' not in tag]
            url = response.urljoin(job.css("h2 > a::attr(href)").get(default="Not specified"))
            item.update(
                title=title,
                salary=salary,
                company=company,
                views=views,
                responses=responses,
                pub_date=pub_date,
                truncate_description=truncate_description,
                tags=tags,
                url=url,
                category=self.category,
            )
            yield item
        yield from self.parse_pagination(response)

    def parse_pagination(self, response):
        """Обробляє пагінацію та переходить на наступну сторінку."""
        next_page_element = response.css("li.page-item.active + li.page-item a.page-link")

        if next_page_element:
            next_page_value = next_page_element.css("::text").get(default="").strip()
            if next_page_value.isdigit():
                next_page_link = next_page_element.css("::attr(href)").get()
                if next_page_link:
                    next_page_url = urllib_urljoin(response.url, next_page_link)
                    self.logger.info(f"➡ Переходимо на сторінку {next_page_value}: {next_page_url}")
                    yield response.follow(
                        next_page_url,
                        callback=self.parse_jobs,
                        meta={"playwright": True, "playwright_context": "default"},
                        dont_filter=True
                    )

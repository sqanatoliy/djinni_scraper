import scrapy
from scrapy_playwright.page import PageMethod
import os

LOGIN_SELECTOR = "ul > li:nth-child(1) > a.jobs-push-login-link"
EMAIL_SELECTOR = "#email"
PASSWORD_SELECTOR = "#password"
LOGIN_BUTTON_SELECTOR = 'button.btn-primary[type="submit"]'
PROFILE_SELECTOR = "div.navbar-collapse div.user-name"


class DjinniSpider(scrapy.Spider):
    name = "djinni"
    allowed_domains = ["djinni.co"]
    start_urls = ["https://djinni.co/jobs/"]
    login_url = "https://djinni.co/"
    username = "your_email@gmail.com"
    password = "your_password"

    def start_requests(self):
        """Перевіряємо, чи є збережений стан. Якщо є — використовуємо, інакше логінимось."""
        if os.path.exists("state.json"):
            self.logger.info("✅ Знайдено state.json! Використовуємо збережену сесію.")
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
                url=self.login_url,
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
        """Процес авторизації"""
        page = response.meta["playwright_page"]

        await page.route("**", self.log_request)
        await page.wait_for_selector(LOGIN_SELECTOR)
        await page.click(LOGIN_SELECTOR)

        await page.wait_for_timeout(3000)
        await page.fill(EMAIL_SELECTOR, self.username)
        await page.fill(PASSWORD_SELECTOR, self.password)
        storage_data = await page.evaluate("JSON.stringify(localStorage)")
        print(f"🟢 LocalStorage: {storage_data}")

        cookies = await page.context.cookies()
        print(f"🟢 Cookies: {cookies}")
        await page.click(LOGIN_BUTTON_SELECTOR)

        await page.wait_for_selector(PROFILE_SELECTOR)

        # Збереження сесії в файл
        await page.context.storage_state(path="state.json")

        await page.close()

        # Тепер перевіряємо чи логін успішний
        yield scrapy.Request(
            url=self.start_urls[0],
            meta={"playwright": True, "playwright_context": "default"},
            callback=self.parse_jobs,
        )

    async def parse_jobs(self, response):
        """Парсимо вакансії після авторизації."""
        jobs = response.css("ul.list-jobs > li")
        for job in jobs:
            title = job.css("h2 > a::text").get(default="Not specified").strip(),
            salary = job.css("h2 > strong > span::text").get(default="Not specified").strip(),
            company = job.css("a.text-body.js-analytics-event::text").get(default="Not specified").strip(),
            views = job.css("div > div:nth-child(2) > span:nth-child(2)::text").get(default="Not specified").strip(),
            responses = job.css("div > div:nth-child(2) > span:nth-child(4)::text").get(default="Not specified").strip(),
            pub_date = job.css("div > div:nth-child(2) > span:nth-child(6)::text").get(default="Not specified").strip(),
            truncate_description = job.css("span.js-truncated-text::text").get(default="Not specified").strip(),
            tags = job.css("h2 + div::text").getall(),
            url = response.urljoin(job.css("h2 > a::attr(href)").get(default="Not specified")),
            yield {
                "title": title,
                "salary": salary,
                "company": company,
                "views": views,
                "responses": responses,
                "pub_date": pub_date,
                "truncate_description": truncate_description,
                "tags": tags,
                "url": url,
            }

        next_page = response.css("a.page-link.next::attr(href)").get()
        if next_page:
            yield response.follow(
                next_page, callback=self.parse_jobs, meta={"playwright": True, "playwright_context": "default"}
            )

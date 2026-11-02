import asyncio
import os
import re
from urllib.parse import parse_qsl, urlencode, urljoin as urllib_urljoin, urlsplit, urlunsplit

import scrapy
from decouple import config
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from scrapy import signals
from scrapy.exceptions import CloseSpider

from djinni_scraper.adapters.djinni_vacancy import normalize_djinni_vacancy
from djinni_scraper.parsers.djinni_jsonld import parse_detail_metadata
from djinni_scraper.search_profiles import SearchProfile, get_search_profile
from djinni_scraper.utils.url_utils import BASE_URL, get_start_url
from ..items import DjinniScraperItem

STATE_FILE = "state.json"
DJINNI_HOME_URL = "https://djinni.co/"
LOGIN_TIMEOUT_MS = 10_000
# Djinni authentication currently requires a fresh tab and short delays between
# browser actions; keep this sequence regression-tested.
DJINNI_AUTH_STEP_DELAY_MS = 2_000
AD_HOC_DISCOVERY_ARGUMENT = "category"
LOGIN_ACTION_NAME = re.compile(r"^(?:Увійти|Вхід|Log In|Login|Sign In)$", re.IGNORECASE)


class LoginUIError(RuntimeError):
    """The expected interactive Djinni login UI was unavailable or unsafe to use."""


class DjinniSelectors:
    # The public landing page may be Ukrainian (/) or the international /hire
    # landing page.  Both expose a visible UI link to the login form, but the
    # accessible name differs (for example, /hire currently says "Sign In").
    LOGIN_ACTION_HREF_SELECTOR = 'a[href*="/login"]:visible'
    LOGIN_ACTION_NAV_SELECTOR = 'nav a[href*="/login"]:visible, header a[href*="/login"]:visible'
    LOGIN_ACTION_FALLBACK_SELECTOR = (
        'a.btn[href*="/login"]:visible, [role="navigation"] a[href*="/login"]:visible, '
        'button[data-bs-target*="login" i]:visible, button[data-target*="login" i]:visible'
    )
    LOGIN_ACTION_DIAGNOSTIC_SELECTOR = 'a[href*="/login"]:visible'
    LOGIN_FORM_SELECTOR = 'form:has(input[name="email"]), form:has(input[type="email"])'
    EMAIL_SELECTOR = 'input[name="email"], input[type="email"]'
    PASSWORD_SELECTOR = 'input[name="password"], input[type="password"]'
    LOGIN_BUTTON_SELECTOR = 'button[type="submit"], input[type="submit"]'
    AUTHENTICATED_MARKER_SELECTOR = 'a[href^="/logout"], a[href^="/my/inbox"]'
    LOGIN_ERROR_SELECTOR = '[role="alert"], .invalid-feedback, .alert-danger'
    LOGIN_CHALLENGE_SELECTOR = (
        'iframe[src*="captcha"], iframe[title*="challenge" i], '
        '[class*="captcha" i], [id*="captcha" i], [data-sitekey]'
    )
    JOBS_LIST_SELECTOR = ".list-jobs .job-item"
    JOB_LINK_SELECTOR = ".job_item__header-link"
    JOB_TITLE_SELECTOR = ".job_item__header-link .job-item__position"
    JOB_COMPANY_SELECTOR = ".job_item__header-link .small.text-gray-800.opacity-75.font-weight-500"
    JOB_DESCRIPTION_SELECTOR = ".js-truncated-text"
    JOB_TAGS_SELECTOR = ".job-item__tags .badge"
    JOB_PUBLICATION_DATE_SELECTOR = "span.text-nowrap[data-bs-original-title]"
    PAGINATION_ITEMS_SELECTOR = "ul.pagination_with_numbers li.page-item"


class DjinniSpider(scrapy.Spider):
    name = "djinni"
    allowed_domains = ["djinni.co"]

    def __init__(self, profile=None, **kwargs):
        super().__init__(**kwargs)

        self.search_profile: SearchProfile | None = None
        self.notification_route: str | None = None
        if profile is not None:
            if kwargs:
                conflicting_args = ", ".join(sorted(kwargs))
                raise ValueError(
                    "Djinni profile mode cannot be combined with ad-hoc search arguments: "
                    f"{conflicting_args}"
                )
            self.search_profile = get_search_profile(profile)
            self.params = dict(self.search_profile.query_params)
            self.discovery_profile = self.search_profile.slug
            self.notification_route = self.search_profile.notification_route
        else:
            self.params = self._ad_hoc_query_params(kwargs)
            self.discovery_profile = self._ad_hoc_discovery_profile(kwargs)

        self.start_urls = [get_start_url(self.params)]

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

    async def start(self):
        """Expose the existing authentication flow through Scrapy's current start API."""
        for request in self.start_requests():
            yield request

    def start_requests(self):
        """Reuses a local session or starts one explicit login attempt."""
        self._log_search_configuration()
        if self._storage_state_exists():
            self.logger.info(f"✅ Знайдено state.json! Використовуємо збережену сесію. URL: {self.start_urls[0]}")
            yield self._jobs_request(self.start_urls[0])
        else:
            self.logger.info("🔄 Виконуємо авторизацію...")
            # Never visit a profile search URL until the browser has confirmed
            # authentication through the visible homepage UI.
            yield self._login_request(self.start_urls[0], auth_recovery_attempted=False)

    async def login(self, response):
        """Authenticates in a fresh tab, then resumes the original jobs URL."""
        initial_page = response.meta.get("playwright_page")
        auth_page = None
        target_url = response.meta.get("auth_target_url", self.start_urls[0])
        email = ""
        password = ""
        step = "initial browser page"
        try:
            if not initial_page:
                raise LoginUIError("Djinni auth did not receive the initial Playwright page")

            email, password = self._get_credentials()
            self.logger.info("Djinni auth: initial browser page ready.")

            step = "initial-page delay"
            self.logger.info("Djinni auth: waiting 2 seconds before creating auth tab.")
            await initial_page.wait_for_timeout(DJINNI_AUTH_STEP_DELAY_MS)

            step = "auth-tab creation"
            auth_page = await initial_page.context.new_page()
            self.logger.info("Djinni auth: new auth tab created.")

            step = "auth-tab delay"
            self.logger.info("Djinni auth: waiting 2 seconds before Djinni navigation.")
            await auth_page.wait_for_timeout(DJINNI_AUTH_STEP_DELAY_MS)

            step = "homepage navigation"
            await auth_page.goto(DJINNI_HOME_URL, wait_until="domcontentloaded")
            await auth_page.wait_for_load_state("domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
            self.logger.info("Djinni auth: homepage opened.")

            step = "homepage login action"
            login_action = await self._login_action(auth_page)
            await self._click(login_action, "homepage login action")
            self.logger.info("Djinni auth: login entry clicked.")

            step = "login form readiness"
            login_form = await self._wait_for_login_form(auth_page)
            self.logger.info("Djinni auth: login form ready.")
            step = "email input"
            email_input = await self._required_form_control(
                login_form, DjinniSelectors.EMAIL_SELECTOR, "email input"
            )
            step = "password input"
            password_input = await self._required_form_control(
                login_form, DjinniSelectors.PASSWORD_SELECTOR, "password input"
            )
            step = "email fill"
            await self._fill_and_verify(email_input, email, "email")
            step = "password fill"
            await self._fill_and_verify(password_input, password, "password")
            self.logger.info("Djinni auth: credentials filled.")

            step = "post-credentials delay"
            self.logger.info("Djinni auth: waiting 2 seconds before submit.")
            await auth_page.wait_for_timeout(DJINNI_AUTH_STEP_DELAY_MS)

            step = "login submit"
            submit = await self._login_submit(login_form)
            step = "login submit click"
            await self._click(submit, "login submit")
            self.logger.info("Djinni auth: login submit clicked.")

            step = "authenticated-state wait"
            if not await self._wait_for_authenticated(auth_page):
                self.logger.warning(
                    "Djinni login did not reach an authenticated state: %s",
                    await self._login_diagnostic(auth_page, email, password),
                )
                self._discard_storage_state()
                failure_reason = (
                    "auth_challenge"
                    if await self._is_visible(auth_page.locator(DjinniSelectors.LOGIN_CHALLENGE_SELECTOR))
                    else "auth_rejected"
                )
                self._record_application_failure(failure_reason)
                raise CloseSpider("Djinni login was rejected or remained anonymous")

            self._record_authenticated()
            try:
                step = "storage state write"
                await auth_page.context.storage_state(path=STATE_FILE)
            except PlaywrightError:
                self.logger.exception("Failed to save confirmed Djinni Playwright storage state.")
                raise

            self.logger.info("Djinni auth: authentication confirmed; local storage state saved.")
            await self._close_initial_page(initial_page)
            initial_page = None
            yield self._jobs_request(
                target_url,
                auth_recovery_attempted=response.meta.get("auth_recovery_attempted", False),
            )
        except LoginUIError as error:
            self._discard_storage_state()
            self._record_application_failure("login_ui_unavailable")
            self.logger.error(
                "Djinni login UI failed: %s; %s",
                error,
                await self._login_diagnostic(auth_page or initial_page, email, password),
            )
            raise CloseSpider("Djinni login UI was unavailable") from error
        except PlaywrightTimeoutError as error:
            self._discard_storage_state()
            self._record_application_failure("login_timeout")
            self.logger.error(
                "Djinni login timed out: %s",
                await self._login_diagnostic(auth_page or initial_page, email, password),
            )
            raise CloseSpider("Djinni login timed out or remained anonymous") from error
        except PlaywrightError as error:
            self._discard_storage_state()
            self._record_infrastructure_failure("playwright_error")
            self.logger.error(
                "Djinni Playwright error during %s (%s): error=%s %s",
                step,
                type(error).__name__,
                self._safe_text(str(error), email, password),
                await self._login_diagnostic(auth_page or initial_page, email, password),
            )
            raise CloseSpider("Djinni login failed due to a Playwright error") from error
        finally:
            await self._close_auth_page(auth_page)
            await self._close_initial_page(initial_page)

    async def parse_jobs(self, response):
        """Парсимо вакансії після авторизації."""
        page = response.meta.get("playwright_page")
        if not page:
            self._record_infrastructure_failure("jobs_page_missing")
            raise CloseSpider("Djinni jobs request did not receive a Playwright page")

        try:
            if not await self._is_authenticated(page):
                self._discard_storage_state()
                if response.meta.get("auth_recovery_attempted", False):
                    self._record_application_failure("auth_recovery_failed")
                    raise CloseSpider("Djinni authentication recovery failed; anonymous jobs page was not parsed")

                target_url = response.meta.get("auth_target_url", response.url)
                self.logger.warning("Djinni session is invalid; starting one login recovery attempt.")
                yield self._login_request(target_url, auth_recovery_attempted=True)
                return

            self.logger.info("✅ Авторизація активна.")
            self._record_authenticated()
            self._record_search_requested(response.url)
            jobs = response.css(DjinniSelectors.JOBS_LIST_SELECTOR)
            self._record_listing_page(len(jobs))
            for job in jobs:
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
        """Obtains an initial managed page; login itself happens only in a fresh tab."""
        self._get_credentials()
        return scrapy.Request(
            url=DJINNI_HOME_URL,
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

    def _get_credentials(self):
        credentials = {
            "DJINNI_EMAIL": config("DJINNI_EMAIL", default="").strip(),
            "DJINNI_PASSWORD": config("DJINNI_PASSWORD", default="").strip(),
        }
        missing = [name for name, value in credentials.items() if not value]
        if missing:
            self._record_application_failure("missing_credentials")
            raise CloseSpider(f"Missing required Djinni configuration: {', '.join(missing)}")
        return credentials["DJINNI_EMAIL"], credentials["DJINNI_PASSWORD"]

    @staticmethod
    def _ad_hoc_query_params(kwargs):
        """Builds ad-hoc filters while reserving ``category`` as discovery provenance."""
        params = {}
        for key, value in kwargs.items():
            if key == AD_HOC_DISCOVERY_ARGUMENT:
                continue
            params[key] = value.split(",") if isinstance(value, str) and "," in value else value
        return params

    @staticmethod
    def _ad_hoc_discovery_profile(kwargs):
        value = kwargs.get(AD_HOC_DISCOVERY_ARGUMENT, kwargs.get("primary_keyword", "all"))
        return value.strip() if isinstance(value, str) and value.strip() else "all"

    def _log_search_configuration(self):
        if not self.search_profile:
            return
        query = self.search_profile.query
        query_log = (
            f"profile={self.search_profile.slug} "
            f"primary_keyword={query.primary_keyword} "
            f"exp_level={','.join(query.exp_level)} "
            f"english_level={','.join(query.english_level)}"
        )
        if query.employment:
            query_log = f"{query_log} employment={','.join(query.employment)}"
        self.logger.info(
            "Djinni %s search_url=%s notification_route=%s",
            query_log,
            self.start_urls[0],
            self.notification_route,
        )

    def _record_authenticated(self):
        self._set_run_stat("djinni/authenticated", True)

    def _record_search_requested(self, url):
        self._set_run_stat("djinni/search_requested", True)
        self._set_run_stat("djinni/search_url", self._safe_url(url))

    def _record_listing_page(self, job_cards):
        self._set_run_stat("djinni/listing_pages", self._run_stat_value("djinni/listing_pages") + 1)
        self._set_run_stat("djinni/listing_job_cards", self._run_stat_value("djinni/listing_job_cards") + job_cards)

    def _record_listing_completed(self):
        self._set_run_stat("djinni/listing_completed", True)

    def _record_application_failure(self, reason):
        self._set_run_stat("djinni/application_failure", reason)

    def _record_infrastructure_failure(self, reason):
        self._set_run_stat("djinni/infrastructure_failure", reason)

    def _set_run_stat(self, key, value):
        crawler = getattr(self, "crawler", None)
        if crawler is not None:
            crawler.stats.set_value(key, value)

    def _run_stat_value(self, key):
        crawler = getattr(self, "crawler", None)
        return crawler.stats.get_value(key, 0) if crawler is not None else 0

    async def _wait_for_authenticated(self, page):
        """Waits for a positive logged-in-only marker or a visible login error, bounded in time."""
        authenticated_marker = page.locator(DjinniSelectors.AUTHENTICATED_MARKER_SELECTOR).first
        login_error = page.locator(DjinniSelectors.LOGIN_ERROR_SELECTOR).first
        login_challenge = page.locator(DjinniSelectors.LOGIN_CHALLENGE_SELECTOR).first
        marker_wait = asyncio.create_task(
            authenticated_marker.wait_for(state="visible", timeout=LOGIN_TIMEOUT_MS)
        )
        error_wait = asyncio.create_task(login_error.wait_for(state="visible", timeout=LOGIN_TIMEOUT_MS))
        challenge_wait = asyncio.create_task(login_challenge.wait_for(state="visible", timeout=LOGIN_TIMEOUT_MS))
        done, pending = await asyncio.wait(
            {marker_wait, error_wait, challenge_wait}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        marker_result, _error_result, _challenge_result = await asyncio.gather(
            marker_wait, error_wait, challenge_wait, return_exceptions=True
        )

        if marker_wait in done and not isinstance(marker_result, BaseException):
            return await self._is_authenticated(page)
        return False

    @staticmethod
    async def _is_authenticated(page):
        """Requires a positive logged-in-only navigation marker, never just a changed URL."""
        marker = page.locator(DjinniSelectors.AUTHENTICATED_MARKER_SELECTOR)
        return await marker.count() > 0 and await marker.first.is_visible()

    async def _login_action(self, page):
        for role in ("link", "button"):
            action = page.get_by_role(role, name=LOGIN_ACTION_NAME)
            count = await action.count()
            if count > 1:
                raise LoginUIError(f"Djinni homepage {role} login action is ambiguous ({count} matches)")
            if count == 1 and await action.is_visible():
                return action

        for label, selector in (
            ("known login href", DjinniSelectors.LOGIN_ACTION_HREF_SELECTOR),
            ("navigation login href", DjinniSelectors.LOGIN_ACTION_NAV_SELECTOR),
            ("login CSS fallback", DjinniSelectors.LOGIN_ACTION_FALLBACK_SELECTOR),
        ):
            action = page.locator(selector)
            count = await action.count()
            if count > 1:
                raise LoginUIError(f"Djinni {label} is ambiguous ({count} visible matches)")
            if count == 1 and await action.is_visible():
                return action
        raise LoginUIError("Djinni homepage has no visible localized login action")

    async def _wait_for_login_form(self, page):
        login_form = page.locator(DjinniSelectors.LOGIN_FORM_SELECTOR)
        try:
            await login_form.wait_for(state="visible", timeout=LOGIN_TIMEOUT_MS)
        except PlaywrightTimeoutError as error:
            raise LoginUIError("Djinni login form did not become visible after the homepage action") from error
        return await self._required_visible(login_form, "login form")

    async def _required_form_control(self, login_form, selector, label, *, enabled=False):
        control = login_form.locator(selector)
        control = await self._required_visible(control, label)
        if enabled and not await self._is_action_enabled(control, label):
            raise LoginUIError(f"Djinni {label} is disabled")
        return control

    async def _login_submit(self, login_form):
        submit = login_form.get_by_role("button", name=LOGIN_ACTION_NAME)
        count = await submit.count()
        if count > 1:
            raise LoginUIError(f"Djinni login submit is ambiguous ({count} matches)")
        if count == 1:
            submit = await self._required_visible(submit, "login submit")
            if not await self._is_action_enabled(submit, "login submit"):
                raise LoginUIError("Djinni login submit is disabled")
            return submit
        return await self._required_form_control(
            login_form, DjinniSelectors.LOGIN_BUTTON_SELECTOR, "login submit", enabled=True
        )

    @staticmethod
    async def _required_visible(locator, label):
        count = await locator.count()
        if count != 1:
            raise LoginUIError(f"Djinni {label} expected exactly one match; found {count}")
        if not await locator.is_visible():
            raise LoginUIError(f"Djinni {label} is not visible")
        return locator

    @staticmethod
    async def _is_action_enabled(locator, label):
        try:
            return await locator.is_enabled()
        except PlaywrightError:
            try:
                disabled = await locator.get_attribute("disabled")
                aria_disabled = await locator.get_attribute("aria-disabled")
            except PlaywrightError as error:
                raise LoginUIError(f"Djinni {label} enabled state could not be determined") from error
            return disabled is None and aria_disabled != "true"

    async def _fill_and_verify(self, locator, value, field_name):
        try:
            await locator.fill(value)
            filled = bool(await locator.input_value())
        except PlaywrightError as error:
            raise LoginUIError(f"Djinni {field_name} input could not be filled") from error
        if not filled:
            raise LoginUIError(f"Djinni {field_name} input did not retain a value")
        self.logger.info("Djinni login %s filled: yes", field_name)

    @staticmethod
    async def _click(locator, label):
        try:
            await locator.click()
        except PlaywrightError as error:
            raise LoginUIError(f"Djinni {label} could not be clicked") from error

    async def _login_diagnostic(self, page, email, password):
        if not page:
            return "page=missing"
        login_form = page.locator(DjinniSelectors.LOGIN_FORM_SELECTOR)
        email_input = login_form.locator(DjinniSelectors.EMAIL_SELECTOR)
        password_input = login_form.locator(DjinniSelectors.PASSWORD_SELECTOR)
        authenticated_marker = page.locator(DjinniSelectors.AUTHENTICATED_MARKER_SELECTOR)
        validation = page.locator(DjinniSelectors.LOGIN_ERROR_SELECTOR)
        challenge = page.locator(DjinniSelectors.LOGIN_CHALLENGE_SELECTOR)
        validation_text = await self._visible_text(validation)
        login_candidates, visible_login_names = await self._login_action_candidates(page)
        return (
            f"url={self._safe_url(page.url)} "
            f"login_action_candidates={login_candidates} "
            f"visible_login_action_names={visible_login_names or 'none'} "
            f"login_form_visible={await self._is_visible(login_form)} "
            f"email_input_visible={await self._is_visible(email_input)} "
            f"password_input_visible={await self._is_visible(password_input)} "
            f"authenticated_marker_visible={await self._is_visible(authenticated_marker)} "
            f"challenge_detected={await self._is_visible(challenge)} "
            f"validation={self._safe_text(validation_text, email, password) if validation_text else 'none'}"
        )

    async def _login_action_candidates(self, page):
        """Returns safe, UI-only evidence when a login entry cannot be found."""
        try:
            candidates = page.locator(DjinniSelectors.LOGIN_ACTION_DIAGNOSTIC_SELECTOR)
            count = await candidates.count()
            names = await candidates.all_text_contents()
        except PlaywrightError:
            return "unknown", "unknown"
        normalized_names = [self._safe_text(name, "", "") for name in names if name and name.strip()]
        return count, ",".join(normalized_names[:5])

    @staticmethod
    async def _is_visible(locator):
        try:
            return await locator.count() > 0 and await locator.first.is_visible()
        except PlaywrightError:
            return False

    async def _visible_text(self, locator):
        if not await self._is_visible(locator):
            return None
        try:
            return await locator.first.text_content()
        except PlaywrightError:
            return None

    @staticmethod
    def _safe_url(value):
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    @staticmethod
    def _safe_text(value, email, password):
        text = " ".join((value or "").split())
        for secret in (email, password):
            if secret:
                text = text.replace(secret, "<redacted>")
        return text[:300]

    async def _close_page(self, page):
        if page:
            try:
                await page.close()
            except PlaywrightError as error:
                self.logger.warning("Unable to close Djinni Playwright page cleanly: %s", error)

    async def _close_auth_page(self, auth_page):
        """Closes only the page created by this login flow, never its context or browser."""
        if auth_page and not auth_page.is_closed():
            await self._close_page(auth_page)

    async def _close_initial_page(self, initial_page):
        """Releases the callback-owned include_page after the auth flow has finished."""
        if initial_page and not initial_page.is_closed():
            await self._close_page(initial_page)

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
        # A missing JobPosting leaves only listing-level fallback fields.  It is
        # safe for a new insert, but must not replace a previously complete row.
        enriched_item["canonical_snapshot_complete"] = detail_metadata.get("raw_jsonld") is not None

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
            company=self._normalize_text(job.css(f"{DjinniSelectors.JOB_COMPANY_SELECTOR}::text").get()),
            listing_published_at=self._normalize_text(
                job.css(f"{DjinniSelectors.JOB_PUBLICATION_DATE_SELECTOR}::attr(data-bs-original-title)").get()
            ),
            listing_description=self._normalize_text(
                job.css(f"{DjinniSelectors.JOB_DESCRIPTION_SELECTOR}::text").get()
            ),
            tags=[tag for tag in tags if tag],
            url=response.urljoin(href) if href else "Not specified",
            discovery_profile=self.discovery_profile,
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
            self._record_listing_completed()
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
        if not next_page_link:
            return None

        next_page_url = urllib_urljoin(response.url, next_page_link)
        current = urlsplit(response.url)
        next_page = urlsplit(next_page_url)
        next_query = parse_qsl(next_page.query, keep_blank_values=True)
        next_page_values = [(key, value) for key, value in next_query if key == "page"]
        current_query = parse_qsl(current.query, keep_blank_values=True)
        # Djinni pagination links can contain only a subset of the active
        # filters.  The profile URL is authoritative; pagination may change
        # just the page number and must never silently narrow its filters.
        preserved_query = [(key, value) for key, value in current_query if key != "page"]
        merged_query = [*preserved_query, *next_page_values] if next_page_values else next_query
        return urlunsplit(
            (
                next_page.scheme,
                next_page.netloc,
                next_page.path,
                urlencode(merged_query, doseq=True),
                next_page.fragment,
            )
        )

    async def errback_close_page(self, failure):
        """Закриває Playwright сторінку у випадку помилки."""
        self._record_infrastructure_failure("playwright_request_error")
        page: Page = failure.request.meta.get("playwright_page")
        if page:
            await page.close()
            self.logger.warning("❌ Сторінка Playwright закрита через помилку.")
        else:
            self.logger.error("🚨 Помилка: не вдалося отримати `playwright_page` у `errback`.")
        raise CloseSpider("Djinni Playwright jobs request failed")

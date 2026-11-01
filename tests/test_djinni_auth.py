import asyncio
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from scrapy import Request
from scrapy.exceptions import CloseSpider
from scrapy.http import HtmlResponse

import djinni_scraper.spiders.djinni as djinni_module
from djinni_scraper.spiders.djinni import (
    DJINNI_AUTH_STEP_DELAY_MS,
    DJINNI_HOME_URL,
    DjinniSelectors,
    DjinniSpider,
    STATE_FILE,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "djinni"
LISTING_URL = "https://djinni.co/jobs/?primary_keyword=Python&exp_level=1y"
TEST_EMAIL = "test@example.invalid"
TEST_PASSWORD = "test-password"


class FakeContext:
    def __init__(self, initial_page, fail_storage_state=False):
        self.initial_page = initial_page
        self.storage_state_paths = []
        self.fail_storage_state = fail_storage_state
        self.new_page_calls = 0
        self.auth_page = None
        self.closed = False

    async def new_page(self):
        self.new_page_calls += 1
        self.initial_page.trace.append("new-page")
        self.auth_page = self.initial_page.new_auth_page(self)
        return self.auth_page

    async def storage_state(self, path):
        self.storage_state_paths.append(path)
        if self.fail_storage_state:
            raise PlaywrightError("cannot write storage state")
        Path(path).write_text("{}")


class FakeLocator:
    def __init__(self, page, kind, fixed_count=None):
        self.page = page
        self.kind = kind
        self.fixed_count = fixed_count

    @property
    def first(self):
        return self

    async def count(self):
        return self.fixed_count if self.fixed_count is not None else self.page.locator_count(self.kind)

    async def is_visible(self):
        if self.fixed_count is not None:
            return self.fixed_count > 0
        return await self.count() > 0 and self.page.locator_visible(self.kind)

    async def is_enabled(self):
        if self.kind == "login_submit" and self.page.non_native_submit:
            raise PlaywrightError("The semantic login action has no native enabled state")
        return self.page.locator_enabled(self.kind)

    async def get_attribute(self, name):
        if name == "disabled" and self.kind == "login_submit" and not self.page.submit_enabled:
            return "disabled"
        if name == "aria-disabled" and self.kind == "login_submit" and not self.page.submit_enabled:
            return "true"
        return None

    async def wait_for(self, *, state, timeout):
        if state == "visible" and await self.is_visible():
            if self.kind == "authenticated_marker":
                self.page.trace.append("verify-auth")
            return
        raise PlaywrightTimeoutError(f"{self.kind} did not become {state}")

    def locator(self, selector):
        if self.kind != "login_form":
            return FakeLocator(self.page, "missing")
        return self.page.login_form_control(selector)

    def get_by_role(self, role, *, name):
        if self.kind != "login_form":
            return FakeLocator(self.page, "missing")
        matches = (
            self.page.submit_available
            and role == "button"
            and bool(name.fullmatch(self.page.submit_label))
        )
        return FakeLocator(self.page, "login_submit", fixed_count=1 if matches else 0)

    async def fill(self, value):
        if not self.page.fillable.get(self.kind, True):
            raise PlaywrightError(f"{self.kind} cannot be filled")
        self.page.filled.append(self.kind)
        self.page.values[self.kind] = value if self.page.retain_values.get(self.kind, True) else ""
        if self.kind == "email_input":
            self.page.trace.append("fill-email")
        elif self.kind == "password_input":
            self.page.trace.append("fill-password")

    async def input_value(self):
        return self.page.values.get(self.kind, "")

    async def click(self):
        self.page.clicked.append(self.kind)
        if self.kind == "homepage_login_action":
            self.page.login_ui_visible = self.page.login_form_available
            self.page.url = "https://djinni.co/login"
            self.page.trace.append("click-login-entry")
        elif self.kind == "login_submit" and not self.page.fail_authentication:
            self.page.authenticated = True
            self.page.login_ui_visible = False
            self.page.url = LISTING_URL
            self.page.trace.append("click-submit")

    async def text_content(self):
        return self.page.validation_text if self.kind == "login_error" else None


class FakePage:
    def __init__(
        self,
        *,
        authenticated=False,
        login_label="Увійти",
        login_role="link",
        login_action_available=True,
        login_form_available=True,
        email_available=True,
        password_available=True,
        submit_available=True,
        submit_enabled=True,
        submit_label="Увійти",
        non_native_submit=False,
        fail_authentication=False,
        validation_text=None,
        fillable=None,
        retain_values=None,
        fail_storage_state=False,
        context=None,
        is_auth_page=False,
        trace=None,
    ):
        self.is_auth_page = is_auth_page
        self.url = LISTING_URL if authenticated else "about:blank"
        self.authenticated = authenticated
        self.login_label = login_label
        self.login_role = login_role
        self.login_action_available = login_action_available
        self.login_form_available = login_form_available
        self.email_available = email_available
        self.password_available = password_available
        self.submit_available = submit_available
        self.submit_enabled = submit_enabled
        self.submit_label = submit_label
        self.non_native_submit = non_native_submit
        self.fail_authentication = fail_authentication
        self.validation_text = validation_text
        self.login_ui_visible = False
        self.fillable = fillable or {}
        self.retain_values = retain_values or {}
        self.fail_storage_state = fail_storage_state
        self.context = context or FakeContext(self, fail_storage_state=fail_storage_state)
        self.closed = False
        self.filled = []
        self.clicked = []
        self.values = {}
        self.wait_timeouts = []
        self.goto_urls = []
        self.load_states = []
        self.homepage_loaded = False
        self.trace = trace if trace is not None else ["initial-page-ready"]

    def new_auth_page(self, context):
        return FakePage(
            login_label=self.login_label,
            login_role=self.login_role,
            login_action_available=self.login_action_available,
            login_form_available=self.login_form_available,
            email_available=self.email_available,
            password_available=self.password_available,
            submit_available=self.submit_available,
            submit_enabled=self.submit_enabled,
            submit_label=self.submit_label,
            non_native_submit=self.non_native_submit,
            fail_authentication=self.fail_authentication,
            validation_text=self.validation_text,
            fillable=self.fillable,
            retain_values=self.retain_values,
            fail_storage_state=self.fail_storage_state,
            context=context,
            is_auth_page=True,
            trace=self.trace,
        )

    async def wait_for_timeout(self, timeout):
        self.wait_timeouts.append(timeout)
        self.trace.append(f"wait-{timeout}")

    async def goto(self, url, *, wait_until):
        assert self.is_auth_page
        assert wait_until == "domcontentloaded"
        self.goto_urls.append(url)
        self.url = url
        self.homepage_loaded = True
        self.trace.append("goto-homepage")

    async def wait_for_load_state(self, state, *, timeout):
        self.load_states.append((state, timeout))

    def get_by_role(self, role, *, name):
        matches = (
            self.is_auth_page
            and self.homepage_loaded
            and self.login_action_available
            and not self.authenticated
            and role == self.login_role
            and bool(name.fullmatch(self.login_label))
        )
        return FakeLocator(self, "homepage_login_action", fixed_count=1 if matches else 0)

    def locator(self, selector):
        if selector == DjinniSelectors.LOGIN_FORM_SELECTOR:
            return FakeLocator(self, "login_form")
        if selector == DjinniSelectors.AUTHENTICATED_MARKER_SELECTOR:
            return FakeLocator(self, "authenticated_marker")
        if selector == DjinniSelectors.LOGIN_ERROR_SELECTOR:
            return FakeLocator(self, "login_error")
        if selector == DjinniSelectors.LOGIN_CHALLENGE_SELECTOR:
            return FakeLocator(self, "login_challenge")
        return FakeLocator(self, "missing")

    def login_form_control(self, selector):
        mapping = {
            DjinniSelectors.EMAIL_SELECTOR: "email_input",
            DjinniSelectors.PASSWORD_SELECTOR: "password_input",
            DjinniSelectors.LOGIN_BUTTON_SELECTOR: "login_submit",
        }
        return FakeLocator(self, mapping.get(selector, "missing"))

    def locator_count(self, kind):
        if kind == "login_form":
            return int(self.login_ui_visible and self.login_form_available and not self.authenticated)
        if kind == "email_input":
            return int(self.login_ui_visible and self.email_available)
        if kind == "password_input":
            return int(self.login_ui_visible and self.password_available)
        if kind == "login_submit":
            return int(self.login_ui_visible and self.submit_available)
        if kind == "authenticated_marker":
            return int(self.authenticated)
        if kind == "login_error":
            return int(bool(self.validation_text))
        if kind == "login_challenge":
            return 0
        return 0

    def locator_visible(self, kind):
        return bool(self.locator_count(kind))

    def locator_enabled(self, kind):
        return kind != "login_submit" or self.submit_enabled

    async def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed


async def collect(async_generator):
    return [result async for result in async_generator]


def make_jobs_response(page, auth_recovery_attempted=False, target_url=LISTING_URL):
    request = Request(
        LISTING_URL,
        meta={
            "playwright_page": page,
            "auth_target_url": target_url,
            "auth_recovery_attempted": auth_recovery_attempted,
        },
    )
    return HtmlResponse(
        url=LISTING_URL,
        body=(FIXTURES_DIR / "jobs_listing.html").read_bytes(),
        encoding="utf-8",
        request=request,
    )


def make_homepage_response(page, target_url=LISTING_URL, auth_recovery_attempted=False):
    request = Request(
        DJINNI_HOME_URL,
        meta={
            "playwright_page": page,
            "auth_target_url": target_url,
            "auth_recovery_attempted": auth_recovery_attempted,
        },
    )
    return HtmlResponse(url=DJINNI_HOME_URL, body=b"<html></html>", request=request)


def assert_close_reason(error, expected_text):
    assert expected_text in error.value.reason


@pytest.fixture
def spider(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        djinni_module,
        "config",
        lambda name, default="": {"DJINNI_EMAIL": TEST_EMAIL, "DJINNI_PASSWORD": TEST_PASSWORD}.get(name, default),
    )
    return DjinniSpider(primary_keyword="Python")


@pytest.mark.parametrize(
    ("fixture_name", "label"),
    (("auth_home_uk.html", "Увійти"), ("auth_home_en.html", "Log In")),
)
def test_login_entry_fixtures_offer_a_specific_localized_navigation_action(fixture_name, label):
    response = HtmlResponse(
        url=DJINNI_HOME_URL,
        body=(FIXTURES_DIR / fixture_name).read_bytes(),
        encoding="utf-8",
    )

    assert response.css('a[href*="/login"]::text').get().strip() == label


def test_login_form_and_authenticated_navigation_fixtures_expose_semantic_contracts():
    login_response = HtmlResponse(
        url="https://djinni.co/login",
        body=(FIXTURES_DIR / "auth_login_form.html").read_bytes(),
        encoding="utf-8",
    )
    authenticated_response = HtmlResponse(
        url=LISTING_URL,
        body=(FIXTURES_DIR / "auth_authenticated_nav.html").read_bytes(),
        encoding="utf-8",
    )
    error_response = HtmlResponse(
        url="https://djinni.co/login",
        body=(FIXTURES_DIR / "auth_validation_error.html").read_bytes(),
        encoding="utf-8",
    )

    assert login_response.css('input[name="email"]').get() is not None
    assert login_response.css('input[name="password"]').get() is not None
    assert login_response.css('button[type="submit"]').get() is not None
    assert authenticated_response.css('a[href^="/my/inbox"]').get() is not None
    assert authenticated_response.css('a[href^="/logout"]').get() is not None
    assert error_response.css('[role="alert"]::text').get().strip() == "Invalid credentials"


def test_missing_state_uses_a_managed_initial_page_and_never_requests_direct_login(spider):
    initial_request = list(spider.start_requests())[0]

    assert initial_request.url == spider.start_urls[0]
    assert initial_request.callback == spider.login
    assert "/login" not in initial_request.url
    assert initial_request.meta["auth_target_url"] == spider.start_urls[0]
    assert initial_request.meta["playwright_include_page"] is True


def test_login_requires_the_initial_playwright_page(spider, caplog):
    response = HtmlResponse(
        url=LISTING_URL,
        body=b"<html></html>",
        request=Request(LISTING_URL, meta={}),
    )

    with pytest.raises(CloseSpider) as error:
        asyncio.run(collect(spider.login(response)))

    assert_close_reason(error, "UI was unavailable")
    assert "initial Playwright page" in caplog.text


def test_auth_flow_creates_only_a_new_page_in_the_existing_context():
    source = (Path(__file__).parents[1] / "djinni_scraper" / "spiders" / "djinni.py").read_text()
    login_source = source[source.index("    async def login"):source.index("    async def parse_jobs")]

    assert "initial_page.context.new_page()" in login_source
    assert ".new_context(" not in login_source
    assert ".launch(" not in login_source


@pytest.mark.parametrize("login_label", ("Увійти", "Log In"))
def test_fresh_auth_tab_login_saves_state_and_resumes_jobs(spider, tmp_path, caplog, login_label):
    initial_page = FakePage(login_label=login_label, submit_label=login_label)

    with caplog.at_level("INFO", logger=spider.logger.name):
        resumed_request = asyncio.run(
            collect(spider.login(make_homepage_response(initial_page, target_url=spider.start_urls[0])))
        )[0]

    auth_page = initial_page.context.auth_page
    assert auth_page is not None
    assert initial_page.context.new_page_calls == 1
    assert initial_page.wait_timeouts == [DJINNI_AUTH_STEP_DELAY_MS]
    assert auth_page.wait_timeouts == [DJINNI_AUTH_STEP_DELAY_MS, DJINNI_AUTH_STEP_DELAY_MS]
    assert auth_page.goto_urls == [DJINNI_HOME_URL]
    assert auth_page.load_states == [("domcontentloaded", djinni_module.LOGIN_TIMEOUT_MS)]
    assert initial_page.goto_urls == []
    assert initial_page.clicked == []
    assert initial_page.filled == []
    assert auth_page.clicked == ["homepage_login_action", "login_submit"]
    assert auth_page.filled == ["email_input", "password_input"]
    assert initial_page.context.storage_state_paths == [STATE_FILE]
    assert (tmp_path / STATE_FILE).exists()
    assert resumed_request.url == spider.start_urls[0]
    assert resumed_request.callback == spider.parse_jobs
    assert initial_page.closed is True
    assert auth_page.closed is True
    assert initial_page.context.closed is False
    assert "login email filled: yes" in caplog.text
    assert "login password filled: yes" in caplog.text
    assert "initial browser page ready" in caplog.text
    assert "new auth tab created" in caplog.text
    assert "homepage opened" in caplog.text
    assert "login entry clicked" in caplog.text
    assert "credentials filled" in caplog.text
    assert "login submit clicked" in caplog.text
    assert "authentication confirmed" in caplog.text
    assert TEST_EMAIL not in caplog.text
    assert TEST_PASSWORD not in caplog.text


def test_fresh_tab_sequence_order_regression(spider):
    initial_page = FakePage()

    asyncio.run(collect(spider.login(make_homepage_response(initial_page))))

    assert initial_page.trace == [
        "initial-page-ready",
        "wait-2000",
        "new-page",
        "wait-2000",
        "goto-homepage",
        "click-login-entry",
        "fill-email",
        "fill-password",
        "wait-2000",
        "click-submit",
        "verify-auth",
    ]


def test_non_native_semantic_submit_uses_attribute_enabled_fallback(spider, tmp_path):
    initial_page = FakePage(non_native_submit=True)

    results = asyncio.run(collect(spider.login(make_homepage_response(initial_page))))

    assert results[0].callback == spider.parse_jobs
    assert initial_page.context.storage_state_paths == [STATE_FILE]
    assert (tmp_path / STATE_FILE).exists()


def test_valid_state_is_reused_and_authenticated_jobs_page_is_parsed(spider, tmp_path):
    (tmp_path / STATE_FILE).write_text("{}")
    initial_request = list(spider.start_requests())[0]
    assert initial_request.url == spider.start_urls[0]
    assert initial_request.callback == spider.parse_jobs

    page = FakePage(authenticated=True)
    results = asyncio.run(collect(spider.parse_jobs(make_jobs_response(page))))

    detail_requests = [result for result in results if result.callback == spider.parse_job_detail]
    assert len(detail_requests) == 3
    assert page.clicked == []
    assert page.closed is True


def test_stale_state_is_deleted_once_then_fresh_tab_login_resumes_original_jobs_url(spider, tmp_path):
    state_file = tmp_path / STATE_FILE
    state_file.write_text("{}")
    original_url = "https://djinni.co/jobs/?primary_keyword=Python&page=2"

    expired_page = FakePage()
    login_request = asyncio.run(
        collect(spider.parse_jobs(make_jobs_response(expired_page, target_url=original_url)))
    )[0]

    assert not state_file.exists()
    assert login_request.url == original_url
    assert login_request.meta["auth_recovery_attempted"] is True
    assert login_request.meta["auth_target_url"] == original_url
    assert expired_page.closed is True

    initial_page = FakePage()
    resumed_request = asyncio.run(collect(spider.login(make_homepage_response(initial_page, original_url, True))))[0]
    assert resumed_request.url == original_url
    assert resumed_request.meta["auth_recovery_attempted"] is True


@pytest.mark.parametrize(
    ("page_kwargs", "expected_log"),
    (
        ({"login_action_available": False}, "no visible localized login action"),
        ({"login_form_available": False}, "login form did not become visible"),
        ({"email_available": False}, "email input expected exactly one match"),
        ({"password_available": False}, "password input expected exactly one match"),
        ({"submit_available": False}, "login submit expected exactly one match"),
        ({"submit_enabled": False}, "login submit is disabled"),
        ({"fillable": {"email_input": False}}, "email input could not be filled"),
        ({"retain_values": {"password_input": False}}, "password input did not retain a value"),
    ),
)
def test_missing_or_unusable_login_ui_fails_clearly_without_writing_state(
    spider, tmp_path, caplog, page_kwargs, expected_log
):
    initial_page = FakePage(**page_kwargs)

    with pytest.raises(CloseSpider) as error:
        asyncio.run(collect(spider.login(make_homepage_response(initial_page))))

    assert_close_reason(error, "UI was unavailable")
    assert expected_log in caplog.text
    assert not (tmp_path / STATE_FILE).exists()
    assert initial_page.context.storage_state_paths == []
    assert initial_page.closed is True
    assert initial_page.context.auth_page.closed is True
    assert initial_page.context.closed is False


def test_failed_submit_logs_sanitized_validation_without_writing_state(spider, tmp_path, caplog):
    initial_page = FakePage(
        fail_authentication=True,
        validation_text=f"Invalid credentials for {TEST_EMAIL}: {TEST_PASSWORD}",
    )

    with pytest.raises(CloseSpider) as error:
        asyncio.run(collect(spider.login(make_homepage_response(initial_page))))

    assert_close_reason(error, "rejected or remained anonymous")
    assert "Invalid credentials for <redacted>: <redacted>" in caplog.text
    assert TEST_EMAIL not in caplog.text
    assert TEST_PASSWORD not in caplog.text
    assert initial_page.context.storage_state_paths == []
    assert not (tmp_path / STATE_FILE).exists()
    assert initial_page.closed is True
    assert initial_page.context.auth_page.closed is True


def test_second_failed_session_validation_does_not_schedule_another_login(spider):
    anonymous_page = FakePage()

    with pytest.raises(CloseSpider) as error:
        asyncio.run(
            collect(spider.parse_jobs(make_jobs_response(anonymous_page, auth_recovery_attempted=True)))
        )

    assert_close_reason(error, "recovery failed")
    assert anonymous_page.closed is True


def test_storage_state_write_failure_is_observable_and_closes_only_the_auth_page(spider):
    initial_page = FakePage(fail_storage_state=True)

    with pytest.raises(CloseSpider) as error:
        asyncio.run(collect(spider.login(make_homepage_response(initial_page))))

    assert_close_reason(error, "Playwright error")
    assert initial_page.context.storage_state_paths == [STATE_FILE]
    assert initial_page.closed is True
    assert initial_page.context.auth_page.closed is True
    assert initial_page.context.closed is False


def test_missing_credentials_fails_with_key_names_only(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(djinni_module, "config", lambda name, default="": default)
    spider = DjinniSpider(primary_keyword="Python")

    with pytest.raises(CloseSpider) as error:
        list(spider.start_requests())

    assert_close_reason(error, "DJINNI_EMAIL, DJINNI_PASSWORD")
    assert TEST_PASSWORD not in error.value.reason


def test_only_https_requests_use_the_playwright_download_handler():
    settings_source = (Path(__file__).parents[1] / "djinni_scraper" / "settings.py").read_text()

    assert '"https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler"' in settings_source
    assert '"http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler"' not in settings_source

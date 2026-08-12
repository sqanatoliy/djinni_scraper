import asyncio
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from scrapy import Request
from scrapy.exceptions import CloseSpider
from scrapy.http import HtmlResponse

import djinni_scraper.spiders.djinni as djinni_module
from djinni_scraper.items import DjinniScraperItem
from djinni_scraper.spiders.djinni import DjinniSelectors, DjinniSpider, LOGIN_URL, STATE_FILE


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "djinni"
LISTING_URL = "https://djinni.co/jobs/?primary_keyword=Python&exp_level=1y"


class FakeLocator:
    def __init__(self, count):
        self._count = count

    async def count(self):
        return self._count


class FakeContext:
    def __init__(self, fail_storage_state=False):
        self.storage_state_paths = []
        self.fail_storage_state = fail_storage_state

    async def storage_state(self, path):
        self.storage_state_paths.append(path)
        if self.fail_storage_state:
            raise PlaywrightError("cannot write storage state")
        Path(path).write_text("{}")


class FakePage:
    def __init__(
        self,
        authenticated=False,
        fail_wait_for=None,
        fail_authentication=False,
        fail_storage_state=False,
    ):
        self.url = LISTING_URL if authenticated else LOGIN_URL
        self.authenticated = authenticated
        self.fail_wait_for = fail_wait_for
        self.fail_authentication = fail_authentication
        self.context = FakeContext(fail_storage_state=fail_storage_state)
        self.closed = False
        self.filled = []
        self.clicked = []

    def locator(self, selector):
        if selector == DjinniSelectors.LOGIN_FORM_SELECTOR:
            return FakeLocator(0 if self.authenticated else 1)
        if selector == DjinniSelectors.AUTHENTICATED_MARKER_SELECTOR:
            return FakeLocator(1 if self.authenticated else 0)
        return FakeLocator(0)

    async def wait_for_selector(self, selector, timeout):
        if self.fail_wait_for == selector:
            raise PlaywrightTimeoutError("selector not available")
        if selector == DjinniSelectors.AUTHENTICATED_MARKER_SELECTOR and not self.authenticated:
            raise PlaywrightTimeoutError("authentication marker not available")

    async def fill(self, selector, value):
        self.filled.append((selector, value))

    async def click(self, selector):
        self.clicked.append(selector)
        if selector == DjinniSelectors.LOGIN_BUTTON_SELECTOR and not self.fail_authentication:
            self.authenticated = True
            self.url = LISTING_URL

    async def close(self):
        self.closed = True


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


def make_login_response(page, target_url=LISTING_URL, auth_recovery_attempted=False):
    request = Request(
        LOGIN_URL,
        meta={
            "playwright_page": page,
            "auth_target_url": target_url,
            "auth_recovery_attempted": auth_recovery_attempted,
        },
    )
    return HtmlResponse(url=LOGIN_URL, body=b"<html></html>", request=request)


def assert_close_reason(error, expected_text):
    assert expected_text in error.value.reason


@pytest.fixture
def spider(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        djinni_module,
        "config",
        lambda name, default="": {"DJINNI_EMAIL": "test@example.invalid", "DJINNI_PASSWORD": "test-password"}.get(name, default),
    )
    return DjinniSpider(primary_keyword="Python")


def test_valid_state_is_reused_and_authenticated_jobs_page_is_parsed(spider, tmp_path):
    (tmp_path / STATE_FILE).write_text("{}")

    initial_request = list(spider.start_requests())[0]
    assert initial_request.url == spider.start_urls[0]
    assert initial_request.callback == spider.parse_jobs

    page = FakePage(authenticated=True)
    results = asyncio.run(collect(spider.parse_jobs(make_jobs_response(page))))

    assert len([result for result in results if isinstance(result, DjinniScraperItem)]) == 3
    assert page.closed is True


def test_missing_state_logs_in_saves_state_and_resumes_requested_jobs_page(spider, tmp_path):
    initial_request = list(spider.start_requests())[0]
    assert initial_request.url == LOGIN_URL
    assert initial_request.meta["auth_target_url"] == spider.start_urls[0]

    page = FakePage()
    resumed_request = asyncio.run(
        collect(spider.login(make_login_response(page, target_url=spider.start_urls[0])))
    )[0]

    assert resumed_request.url == spider.start_urls[0]
    assert resumed_request.callback == spider.parse_jobs
    assert page.context.storage_state_paths == [STATE_FILE]
    assert (tmp_path / STATE_FILE).exists()
    assert page.closed is True


def test_expired_state_is_removed_then_one_login_resumes_original_url(spider, tmp_path):
    state_file = tmp_path / STATE_FILE
    state_file.write_text("{}")
    original_url = "https://djinni.co/jobs/?primary_keyword=Python&page=2"

    expired_page = FakePage()
    login_request = asyncio.run(
        collect(spider.parse_jobs(make_jobs_response(expired_page, target_url=original_url)))
    )[0]

    assert not state_file.exists()
    assert login_request.url == LOGIN_URL
    assert login_request.meta["auth_recovery_attempted"] is True
    assert login_request.meta["auth_target_url"] == original_url
    assert expired_page.closed is True

    login_page = FakePage()
    resumed_request = asyncio.run(collect(spider.login(make_login_response(login_page, original_url, True))))[0]

    assert resumed_request.url == original_url
    assert resumed_request.meta["auth_recovery_attempted"] is True
    assert login_page.context.storage_state_paths == [STATE_FILE]


def test_failed_reauthentication_never_parses_anonymous_jobs_page(spider, tmp_path):
    (tmp_path / STATE_FILE).write_text("{}")
    expired_page = FakePage()
    login_request = asyncio.run(collect(spider.parse_jobs(make_jobs_response(expired_page))))[0]

    rejected_login_page = FakePage(fail_authentication=True)
    with pytest.raises(CloseSpider) as error:
        asyncio.run(
            collect(
                spider.login(
                    make_login_response(
                        rejected_login_page,
                        target_url=login_request.meta["auth_target_url"],
                        auth_recovery_attempted=True,
                    )
                )
            )
        )

    assert_close_reason(error, "rejected or remained anonymous")
    assert rejected_login_page.closed is True


def test_missing_credentials_fails_with_key_names_only(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(djinni_module, "config", lambda name, default="": default)
    spider = DjinniSpider(primary_keyword="Python")

    with pytest.raises(CloseSpider) as error:
        list(spider.start_requests())

    assert_close_reason(error, "DJINNI_EMAIL, DJINNI_PASSWORD")
    assert "test-password" not in error.value.reason


def test_second_failed_session_validation_does_not_schedule_another_login(spider):
    anonymous_page = FakePage()

    with pytest.raises(CloseSpider) as error:
        asyncio.run(
            collect(
                spider.parse_jobs(
                    make_jobs_response(anonymous_page, auth_recovery_attempted=True)
                )
            )
        )

    assert_close_reason(error, "recovery failed")
    assert anonymous_page.closed is True


def test_login_exception_closes_the_page(spider):
    page = FakePage(fail_wait_for=DjinniSelectors.LOGIN_FORM_SELECTOR)

    with pytest.raises(CloseSpider) as error:
        asyncio.run(collect(spider.login(make_login_response(page))))

    assert_close_reason(error, "timed out")
    assert page.closed is True


def test_storage_state_write_failure_is_observable_and_closes_the_page(spider):
    page = FakePage(fail_storage_state=True)

    with pytest.raises(CloseSpider) as error:
        asyncio.run(collect(spider.login(make_login_response(page))))

    assert_close_reason(error, "Playwright error")
    assert page.context.storage_state_paths == [STATE_FILE]
    assert page.closed is True

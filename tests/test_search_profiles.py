from dataclasses import FrozenInstanceError
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from scrapy.http import HtmlResponse

from djinni_scraper.search_profiles import (
    COMMON_ENGLISH_LEVELS,
    DJINNI_SOURCE,
    DjinniSearchQuery,
    SEARCH_PROFILES,
    SearchProfile,
    _build_registry,
    get_search_profile,
    list_search_profiles,
)
from djinni_scraper.spiders.djinni import DjinniSelectors, DjinniSpider
from djinni_scraper.utils.url_utils import BASE_URL, get_start_url


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "djinni"
DETAIL_URL = "https://djinni.co/jobs/842583-strong-junior-middle-ai-engineer/"
EXPECTED_PROFILES = {
    "python": {"primary_keyword": ["Python"], "exp_level": ["no_exp", "1y", "2y"]},
    "data_engineer": {"primary_keyword": ["Data Engineer"], "exp_level": ["no_exp", "1y"]},
    "other": {"primary_keyword": ["Other"], "exp_level": ["no_exp", "1y"]},
    "support": {"primary_keyword": ["Support"], "exp_level": ["no_exp", "1y"]},
}


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _listing_response() -> HtmlResponse:
    return HtmlResponse(
        url="https://djinni.co/jobs/?primary_keyword=Python&exp_level=1y",
        body=(FIXTURES_DIR / "jobs_listing.html").read_bytes(),
        encoding="utf-8",
    )


def _detail_response() -> HtmlResponse:
    return HtmlResponse(
        url=DETAIL_URL,
        body=(FIXTURES_DIR / "job_detail_full.html").read_bytes(),
        encoding="utf-8",
    )


def test_registry_has_exactly_the_four_formal_djinni_profiles():
    profiles = list_search_profiles()

    assert tuple(profile.slug for profile in profiles) == ("python", "data_engineer", "other", "support")
    assert tuple(SEARCH_PROFILES) == ("python", "data_engineer", "other", "support")
    assert len({profile.slug for profile in profiles}) == len(profiles)
    assert all(profile.source == DJINNI_SOURCE for profile in profiles)


@pytest.mark.parametrize("slug", ("python", "data_engineer", "other", "support"))
def test_formal_profile_query_preserves_current_filters_and_english_levels(slug):
    profile = get_search_profile(slug)

    assert _query(get_start_url(profile.query_params)) == {
        **EXPECTED_PROFILES[slug],
        "english_level": list(COMMON_ENGLISH_LEVELS),
    }
    assert profile.query.employment == ()
    assert "employment" not in profile.query_params


def test_notification_routes_preserve_historical_main_and_alternate_groups():
    assert get_search_profile("python").notification_route == "main"
    assert get_search_profile("data_engineer").notification_route == "main"
    assert get_search_profile("other").notification_route == "alternate"
    assert get_search_profile("support").notification_route == "alternate"


def test_data_engineer_url_encodes_once_and_keeps_the_current_jobs_base_url():
    url = get_start_url(get_search_profile("data_engineer").query_params)

    assert urlsplit(url).scheme == "https"
    assert urlsplit(url).netloc == "djinni.co"
    assert urlsplit(url).path == "/jobs/"
    assert _query(url)["primary_keyword"] == ["Data Engineer"]
    assert "%2520" not in url


def test_profiles_do_not_contain_secrets_and_profile_module_has_no_runtime_dependencies():
    values = " ".join(
        value
        for profile in list_search_profiles()
        for value in (
            profile.slug,
            profile.source,
            profile.query.primary_keyword,
            *profile.query.exp_level,
            *profile.query.english_level,
            profile.notification_route,
        )
    ).casefold()
    assert "token" not in values
    assert "chat" not in values
    assert "telegram" not in values

    source = (Path(__file__).parents[1] / "djinni_scraper" / "search_profiles.py").read_text()
    assert "sqlite" not in source.casefold()
    assert "import scrapy" not in source
    assert "playwright" not in source.casefold()
    assert "telegram_utils" not in source


def test_profiles_and_query_parameters_are_immutable():
    profile = get_search_profile("python")

    with pytest.raises(FrozenInstanceError):
        profile.slug = "changed"
    with pytest.raises(FrozenInstanceError):
        profile.query.exp_level = ("all",)
    with pytest.raises(FrozenInstanceError):
        profile.query.employment = ("remote",)
    with pytest.raises(TypeError):
        profile.query_params["exp_level"] = ("all",)
    assert get_search_profile("python").query.exp_level == ("no_exp", "1y", "2y")
    assert get_search_profile("python").query.employment == ()


def test_employment_defaults_to_empty_and_serializes_only_when_present():
    default_query = DjinniSearchQuery(
        primary_keyword="Data Engineer",
        exp_level=("1y", "2y"),
        english_level=COMMON_ENGLISH_LEVELS,
    )
    remote_query = DjinniSearchQuery(
        primary_keyword="Data Engineer",
        exp_level=("1y", "2y"),
        english_level=COMMON_ENGLISH_LEVELS,
        employment=("remote",),
    )
    multiple_employment_query = DjinniSearchQuery(
        primary_keyword="Data Engineer",
        exp_level=("1y", "2y"),
        english_level=COMMON_ENGLISH_LEVELS,
        employment=("remote", "office"),
    )

    assert default_query.employment == ()
    assert "employment" not in _query(get_start_url(default_query.to_params()))
    assert remote_query.employment == ("remote",)
    assert _query(get_start_url(remote_query.to_params()))["employment"] == ["remote"]
    assert _query(get_start_url(multiple_employment_query.to_params()))["employment"] == ["remote", "office"]


def test_registry_builder_rejects_duplicate_slugs_and_lookup_is_actionable():
    profile = get_search_profile("python")
    with pytest.raises(ValueError, match="Duplicate"):
        _build_registry((profile, profile))
    with pytest.raises(ValueError, match="Unknown Djinni search profile: 'foobar'. Available profiles: python, data_engineer, other, support"):
        get_search_profile("foobar")


def test_profile_validation_requires_meaningful_djinni_configuration():
    with pytest.raises(ValueError, match="primary_keyword"):
        DjinniSearchQuery(primary_keyword="", exp_level=("1y",), english_level=("basic",))
    with pytest.raises(ValueError, match="exp_level"):
        DjinniSearchQuery(primary_keyword="Python", exp_level=(), english_level=("basic",))
    with pytest.raises(ValueError, match="source"):
        SearchProfile(
            slug="external",
            source="other-source",
            query=DjinniSearchQuery(primary_keyword="Python", exp_level=("1y",), english_level=("basic",)),
            notification_route="main",
        )


@pytest.mark.parametrize("slug", ("python", "data_engineer", "other", "support"))
def test_profile_cli_resolves_profile_and_sets_canonical_discovery_slug(slug):
    spider = DjinniSpider(profile=slug)

    assert spider.search_profile == get_search_profile(slug)
    assert spider.category == slug
    assert spider.notification_route == get_search_profile(slug).notification_route
    assert _query(spider.start_urls[0]) == _query(get_start_url(get_search_profile(slug).query_params))


def test_profile_cli_rejects_unknown_and_mixed_legacy_arguments():
    with pytest.raises(ValueError, match="Unknown Djinni search profile"):
        DjinniSpider(profile="foobar")
    with pytest.raises(ValueError, match="cannot be combined with legacy search arguments: primary_keyword"):
        DjinniSpider(profile="python", primary_keyword="DevOps")
    with pytest.raises(ValueError, match="cannot be combined with legacy search arguments: employment"):
        DjinniSpider(profile="data_engineer", employment="remote")


def test_no_argument_and_legacy_ad_hoc_cli_behavior_are_explicit_and_preserved():
    default_spider = DjinniSpider()
    assert default_spider.search_profile is None
    assert default_spider.category == "all"
    assert default_spider.start_urls == [f"{BASE_URL}?"]

    legacy_spider = DjinniSpider(
        primary_keyword="DevOps",
        exp_level="no_exp,2y",
        english_level="intermediate",
        employment="remote",
        category="devops",
    )
    assert legacy_spider.search_profile is None
    assert legacy_spider.category == "devops"
    assert _query(legacy_spider.start_urls[0]) == {
        "primary_keyword": ["DevOps"],
        "exp_level": ["no_exp", "2y"],
        "english_level": ["intermediate"],
        "employment": ["remote"],
    }
    assert "%25" not in legacy_spider.start_urls[0]

    employment_only_spider = DjinniSpider(primary_keyword="DevOps", employment="remote")
    assert employment_only_spider.category == "DevOps"
    assert _query(employment_only_spider.start_urls[0])["employment"] == ["remote"]


def test_profile_slug_reaches_listing_detail_and_canonical_discovery_without_overwriting_job_category():
    spider = DjinniSpider(profile="data_engineer")
    listing_response = _listing_response()
    listing_item = spider._parse_job_card(listing_response.css(DjinniSelectors.JOBS_LIST_SELECTOR)[0], listing_response)

    assert listing_item["category"] == "data_engineer"
    request = spider._detail_request(listing_item)
    assert request.cb_kwargs["listing_item"]["category"] == "data_engineer"

    enriched_item = list(spider.parse_job_detail(_detail_response(), listing_item))[0]
    canonical_vacancy = enriched_item["canonical_vacancy"]
    assert canonical_vacancy.discovery_profiles == ("data_engineer",)
    assert canonical_vacancy.category == "Python"


def test_custom_employment_filter_keeps_category_as_discovery_slug_only():
    spider = DjinniSpider(
        primary_keyword="Data Engineer",
        exp_level="1y,2y",
        english_level="no_english,basic,pre,intermediate",
        employment="remote",
        category="data_engineer_remote",
    )
    listing_response = _listing_response()
    listing_item = spider._parse_job_card(listing_response.css(DjinniSelectors.JOBS_LIST_SELECTOR)[0], listing_response)

    assert _query(spider.start_urls[0])["employment"] == ["remote"]
    assert "category" not in _query(spider.start_urls[0])
    assert listing_item["category"] == "data_engineer_remote"

    enriched_item = list(spider.parse_job_detail(_detail_response(), listing_item))[0]
    assert enriched_item["canonical_vacancy"].discovery_profiles == ("data_engineer_remote",)
    assert enriched_item["canonical_vacancy"].category == "Python"


def test_profile_start_logs_concise_query_configuration(monkeypatch, caplog):
    spider = DjinniSpider(profile="python")
    monkeypatch.setattr(spider, "_storage_state_exists", lambda: True)

    with caplog.at_level("INFO", logger=spider.logger.name):
        list(spider.start_requests())
    assert "profile=python" in caplog.text
    assert "primary_keyword=Python" in caplog.text
    assert "exp_level=no_exp,1y,2y" in caplog.text
    assert "english_level=no_english,basic,pre,intermediate" in caplog.text
    assert "employment=" not in caplog.text


def test_workflow_schedules_the_four_profiles_without_repeating_formal_query_values():
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "djinni_scraper.yml").read_text()

    def step_block(name: str) -> str:
        start = workflow.index(name)
        end = workflow.find("\n    - name:", start + 1)
        return workflow[start:] if end == -1 else workflow[start:end]

    for slug in ("python", "data_engineer", "other", "support"):
        assert f"scrapy crawl djinni -a profile={slug}" in workflow
    assert "-a primary_keyword=" not in workflow
    assert "-a exp_level=" not in workflow
    assert "-a english_level=" not in workflow
    assert "secrets.TELEGRAM_TOKEN" in workflow
    assert "secrets.NO_EXP_TELEGRAM_TOKEN" in workflow
    assert "secrets.TELEGRAM_TOKEN" in step_block("Run Djinni scraper for PYTHON jobs")
    assert "secrets.TELEGRAM_TOKEN" in step_block("Run Djinni scraper for DATA Engineering jobs")
    assert "secrets.NO_EXP_TELEGRAM_TOKEN" in step_block("Run Djinni scraper for OTHER category jobs")
    assert "secrets.NO_EXP_TELEGRAM_TOKEN" in step_block("Run Djinni scraper for SUPPORT jobs")

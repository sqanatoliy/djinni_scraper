from urllib.parse import parse_qsl, urlsplit

import pytest

from djinni_scraper.dou_profiles import (
    DOU_SEARCH_PROFILES,
    DOU_SOURCE,
    DouSearchProfile,
    DouSearchQuery,
    get_dou_search_profile,
    list_dou_search_profiles,
)


EXPECTED_PROFILES = (
    ("dou-python-0-1", "/vacancies/", {"category": "Python", "exp": "0-1"}, "main"),
    ("dou-python-1-3", "/vacancies/", {"category": "Python", "exp": "1-3"}, "main"),
    (
        "dou-data-engineer-0-1",
        "/vacancies/",
        {"category": "Data Engineer", "exp": "0-1"},
        "main",
    ),
    (
        "dou-data-engineer-1-3",
        "/vacancies/",
        {"category": "Data Engineer", "exp": "1-3"},
        "main",
    ),
    ("dou-first-job-remote", "/first-job/", {"city": "remote"}, "alternate"),
    (
        "dou-support-remote-0-1",
        "/vacancies/",
        {"category": "Support", "exp": "0-1"},
        "alternate",
    ),
)


def test_dou_registry_has_exact_stable_source_qualified_profiles_and_routes():
    profiles = list_dou_search_profiles()

    assert tuple(profile.slug for profile in profiles) == tuple(row[0] for row in EXPECTED_PROFILES)
    assert tuple(DOU_SEARCH_PROFILES) == tuple(row[0] for row in EXPECTED_PROFILES)
    assert all(profile.source == DOU_SOURCE for profile in profiles)
    assert all(get_dou_search_profile(profile.slug) is profile for profile in profiles)


@pytest.mark.parametrize(("slug", "path", "expected_query", "route"), EXPECTED_PROFILES)
def test_profile_urls_preserve_each_filter_and_notification_route(slug, path, expected_query, route):
    profile = get_dou_search_profile(slug)
    parsed = urlsplit(profile.search_url)

    assert parsed.scheme == "https"
    assert parsed.netloc == "jobs.dou.ua"
    assert parsed.path == path
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if slug == "dou-support-remote-0-1":
        assert query.pop("remote") == ""
    assert query == expected_query
    assert profile.notification_route == route


def test_support_remote_url_keeps_remote_as_a_bare_dou_flag():
    profile = get_dou_search_profile("dou-support-remote-0-1")
    query_parts = urlsplit(profile.search_url).query.split("&")

    assert query_parts[0] == "remote"
    assert {"category=Support", "exp=0-1"}.issubset(query_parts)
    assert profile.xhr_url == "https://jobs.dou.ua/vacancies/xhr-load/?remote&category=Support&exp=0-1"


def test_first_job_url_uses_city_only_and_has_no_static_vacancies_xhr_url():
    profile = get_dou_search_profile("dou-first-job-remote")
    query_parts = urlsplit(profile.search_url).query.split("&")

    assert profile.search_url == "https://jobs.dou.ua/first-job/?city=remote"
    assert query_parts == ["city=remote"]
    assert profile.xhr_url is None


@pytest.mark.parametrize(
    "kwargs",
    (
        {"remote": True, "city": "Київ"},
        {"remote": True, "relocation": True},
        {"first_job": True, "city": "remote", "category": "Python"},
        {"first_job": True, "city": "remote", "experience": "0-1"},
        {"first_job": True, "remote": True},
        {"first_job": True},
        {"category": " "},
        {"remote": "yes"},
    ),
)
def test_invalid_or_legacy_incompatible_query_combinations_are_rejected(kwargs):
    with pytest.raises(ValueError):
        DouSearchQuery(**kwargs)


def test_profile_model_rejects_another_source_and_unknown_profile_is_actionable():
    with pytest.raises(ValueError, match="source"):
        DouSearchProfile(
            slug="bad",
            source="djinni",  # type: ignore[arg-type]
            notification_route="main",
            query=DouSearchQuery(category="Python"),
        )
    with pytest.raises(ValueError, match="Unknown DOU search profile"):
        get_dou_search_profile("other")

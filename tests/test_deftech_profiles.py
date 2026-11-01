from urllib.parse import parse_qsl, urlsplit

import pytest

from djinni_scraper.deftech_profiles import (
    DEFTECH_SEARCH_PROFILES,
    DOU_SOURCE,
    DefTechSearchProfile,
    DefTechSearchQuery,
    get_deftech_search_profile,
    list_deftech_search_profiles,
)


def test_registry_has_only_the_authorized_initial_profile_with_dou_identity():
    profiles = list_deftech_search_profiles()

    assert tuple(DEFTECH_SEARCH_PROFILES) == ("deftech-data-engineer",)
    assert profiles == (get_deftech_search_profile("deftech-data-engineer"),)
    assert profiles[0].source == DOU_SOURCE
    assert profiles[0].notification_route == "main"
    assert profiles[0].query == DefTechSearchQuery(category="Data Engineer")


@pytest.mark.parametrize(
    ("query", "expected_parts"),
    (
        (DefTechSearchQuery(category="Data Engineer"), ("category=Data+Engineer",)),
        (DefTechSearchQuery(category="Data Engineer", experience="0-1"), ("category=Data+Engineer", "exp=0-1")),
        (DefTechSearchQuery(category="Data Engineer", city="Київ"), ("city=%D0%9A%D0%B8%D1%97%D0%B2", "category=Data+Engineer")),
        (DefTechSearchQuery(category="Data Engineer", remote=True), ("remote", "category=Data+Engineer")),
        (DefTechSearchQuery(search="Python developer"), ("search=Python+developer",)),
        (DefTechSearchQuery(search="Python", search_in_description=True), ("search=Python", "descr=1")),
        (
            DefTechSearchQuery(
                category="Data Engineer",
                experience="0-1",
                city="Київ",
                remote=True,
                search="ETL",
                search_in_description=True,
            ),
            ("remote", "city=%D0%9A%D0%B8%D1%97%D0%B2", "category=Data+Engineer", "exp=0-1", "search=ETL", "descr=1"),
        ),
    ),
)
def test_url_builder_preserves_all_live_confirmed_filter_semantics(query, expected_parts):
    assert query.query_parts() == expected_parts
    assert query.build_url() == f"https://deftech.dou.ua/jobs/?{'&'.join(expected_parts)}"
    assert query.build_url(xhr=True) == f"https://deftech.dou.ua/jobs/xhr-load/?{'&'.join(expected_parts)}"


def test_initial_profile_url_is_deftech_jobs_category_and_not_an_extra_profile():
    profile = get_deftech_search_profile("deftech-data-engineer")
    parsed = urlsplit(profile.search_url)

    assert parsed.scheme == "https"
    assert parsed.netloc == "deftech.dou.ua"
    assert parsed.path == "/jobs/"
    assert dict(parse_qsl(parsed.query, keep_blank_values=True)) == {"category": "Data Engineer"}


@pytest.mark.parametrize(
    "kwargs",
    (
        {"category": " "},
        {"experience": ""},
        {"city": "\t"},
        {"search": "\n"},
        {"remote": "yes"},
        {"search_in_description": 1},
    ),
)
def test_invalid_query_values_are_rejected(kwargs):
    with pytest.raises(ValueError):
        DefTechSearchQuery(**kwargs)


def test_profile_rejects_a_non_dou_canonical_source_and_unknown_slug():
    with pytest.raises(ValueError, match="source"):
        DefTechSearchProfile(
            slug="bad",
            source="deftech",  # type: ignore[arg-type]
            notification_route="main",
            query=DefTechSearchQuery(category="Data Engineer"),
        )
    with pytest.raises(ValueError, match="Unknown DefTech search profile"):
        get_deftech_search_profile("deftech-python")

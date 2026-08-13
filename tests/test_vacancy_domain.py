import json
from dataclasses import FrozenInstanceError, fields
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from djinni_scraper.domain.vacancy import Company, Compensation, LanguageRequirement, Location, Vacancy


SCRAPED_AT = datetime(2026, 8, 13, 9, 30, tzinfo=timezone.utc)


def minimal_vacancy(**overrides) -> Vacancy:
    values = {
        "source": "example",
        "url": "https://example.invalid/jobs/1",
        "title": "Engineer",
        "scraped_at": SCRAPED_AT,
    }
    values.update(overrides)
    return Vacancy(**values)


def test_minimal_valid_vacancy_has_immutable_empty_collections():
    vacancy = minimal_vacancy()

    assert vacancy.source_vacancy_id is None
    assert vacancy.locations == ()
    assert vacancy.raw_source_metadata == {}
    with pytest.raises(FrozenInstanceError):
        vacancy.title = "Changed"


def test_full_vacancy_preserves_nested_value_objects_and_raw_metadata():
    vacancy = minimal_vacancy(
        source_vacancy_id="42",
        description="<p>Full description</p>",
        company=Company(name="Example", url="https://example.invalid"),
        compensation=Compensation(min_amount=1000, max_amount=2000, currency="USD", unit="MONTH"),
        locations=[Location(country="Ukraine", city="Kyiv")],
        language_requirements=[LanguageRequirement(language="English", level="B2")],
        tags=["Product company"],
        discovery_profiles=["python"],
        published_at=date(2026, 8, 12),
        raw_source_metadata={"jsonld": {"identifier": 42}},
    )

    assert vacancy.locations == (Location(country="Ukraine", city="Kyiv"),)
    assert vacancy.tags == ("Product company",)
    assert vacancy.raw_source_metadata["jsonld"]["identifier"] == 42
    with pytest.raises(TypeError):
        vacancy.raw_source_metadata["jsonld"] = {}


@pytest.mark.parametrize("field_name", ["source", "url", "title"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_required_identity_and_content_fields_are_validated(field_name, value):
    with pytest.raises(ValueError):
        minimal_vacancy(**{field_name: value})


def test_scraped_at_must_be_timezone_aware():
    with pytest.raises(ValueError, match="timezone-aware"):
        minimal_vacancy(scraped_at=datetime(2026, 8, 13, 9, 30))


def test_source_id_and_raw_metadata_require_canonical_serializable_values():
    with pytest.raises(ValueError, match="source_vacancy_id"):
        minimal_vacancy(source_vacancy_id=42)
    with pytest.raises(ValueError, match="JSON-compatible"):
        minimal_vacancy(raw_source_metadata={"unexpected": object()})


def test_to_dict_is_json_compatible_and_preserves_datetime_timezone():
    vacancy = minimal_vacancy(
        company=Company(name="Example"),
        compensation=Compensation(min_amount=1000, currency="USD"),
        locations=(Location(country="Ukraine", city="Lviv"),),
        published_at=datetime(2026, 8, 12, 14, 1, tzinfo=timezone.utc),
        raw_source_metadata={"site": {"flag": True}},
    )

    serialized = vacancy.to_dict()
    assert serialized["scraped_at"] == "2026-08-13T09:30:00+00:00"
    assert serialized["published_at"] == "2026-08-12T14:01:00+00:00"
    assert serialized["compensation"] == {
        "min_amount": 1000,
        "max_amount": None,
        "currency": "USD",
        "unit": None,
    }
    assert serialized["locations"] == [{"country": "Ukraine", "city": "Lviv", "region": None}]
    assert json.loads(json.dumps(serialized)) == serialized


def test_domain_model_does_not_leak_known_djinni_fields_or_dependencies():
    field_names = {field_.name for field_ in fields(Vacancy)}
    assert {"truncate_description", "detail_title", "pub_date", "raw_jsonld", "raw_djinni_metadata"}.isdisjoint(
        field_names
    )

    source = (Path(__file__).parents[1] / "djinni_scraper" / "domain" / "vacancy.py").read_text()
    assert "import scrapy" not in source
    assert "import playwright" not in source
    assert "djinni_scraper.items" not in source
    assert "djinni_scraper.parsers" not in source

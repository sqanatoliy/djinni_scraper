"""Lightweight import-direction and source-DTO contract checks."""

import ast
from pathlib import Path

from djinni_scraper.items import DjinniScraperItem


ROOT = Path(__file__).parents[1] / "djinni_scraper"


def _imports_in(directory: Path) -> set[str]:
    modules = set()
    for path in directory.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
    return modules


def _imports_prefix(modules: set[str], prefix: str) -> bool:
    return any(module == prefix or module.startswith(f"{prefix}.") for module in modules)


def test_source_dto_contains_only_current_parser_to_adapter_fields():
    assert set(DjinniScraperItem.fields) == {
        "title",
        "company",
        "listing_published_at",
        "listing_description",
        "tags",
        "url",
        "discovery_profile",
        "source_vacancy_id",
        "detail_title",
        "canonical_url",
        "date_posted",
        "valid_through",
        "salary_currency",
        "salary_min",
        "salary_max",
        "salary_unit",
        "employment_type",
        "experience_months",
        "company_name",
        "company_url",
        "industry",
        "job_category",
        "location_countries",
        "location_cities",
        "applicant_locations",
        "description",
        "raw_jsonld",
        "raw_djinni_metadata",
        "canonical_vacancy",
        "canonical_snapshot_complete",
    }


def test_lower_layers_do_not_import_djinni_source_implementation():
    lower_layer_imports = _imports_in(ROOT / "persistence") | _imports_in(ROOT / "notifications")

    for forbidden_prefix in (
        "djinni_scraper.spiders",
        "djinni_scraper.parsers",
        "djinni_scraper.adapters",
        "djinni_scraper.items",
    ):
        assert not _imports_prefix(lower_layer_imports, forbidden_prefix)


def test_domain_has_no_framework_storage_or_transport_imports():
    domain_imports = _imports_in(ROOT / "domain")

    for forbidden_prefix in ("scrapy", "sqlite3", "requests", "playwright", "djinni_scraper.notifications"):
        assert not _imports_prefix(domain_imports, forbidden_prefix)

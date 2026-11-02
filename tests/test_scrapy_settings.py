from scrapy.utils.project import get_project_settings


def test_project_settings_exclude_unused_feed_exporter_without_importing_it(monkeypatch):
    monkeypatch.setenv("SCRAPY_SETTINGS_MODULE", "djinni_scraper.settings")

    settings = get_project_settings()
    extensions = settings.get_component_priority_dict_with_base("EXTENSIONS")

    assert "scrapy.extensions.feedexport.FeedExporter" not in extensions

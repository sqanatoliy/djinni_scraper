"""Djinni-only Scrapy transport object used before canonical normalization."""

import scrapy


class DjinniScraperItem(scrapy.Item):
    """Carries source parsing evidence; ``Vacancy`` is the domain boundary."""

    title = scrapy.Field()
    company = scrapy.Field()
    listing_published_at = scrapy.Field()
    listing_description = scrapy.Field()
    tags = scrapy.Field()
    url = scrapy.Field()
    discovery_profile = scrapy.Field()
    source_vacancy_id = scrapy.Field()
    detail_title = scrapy.Field()
    canonical_url = scrapy.Field()
    date_posted = scrapy.Field()
    valid_through = scrapy.Field()
    salary_currency = scrapy.Field()
    salary_min = scrapy.Field()
    salary_max = scrapy.Field()
    salary_unit = scrapy.Field()
    employment_type = scrapy.Field()
    experience_months = scrapy.Field()
    company_name = scrapy.Field()
    company_url = scrapy.Field()
    industry = scrapy.Field()
    job_category = scrapy.Field()
    location_countries = scrapy.Field()
    location_cities = scrapy.Field()
    applicant_locations = scrapy.Field()
    description = scrapy.Field()
    raw_jsonld = scrapy.Field()
    raw_djinni_metadata = scrapy.Field()
    canonical_vacancy = scrapy.Field()
    canonical_snapshot_complete = scrapy.Field()

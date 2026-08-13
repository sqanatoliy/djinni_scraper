# Define here the models for your scraped items
#
# See documentation in:
# https://docs.scrapy.org/en/latest/topics/items.html

import scrapy


class DjinniScraperItem(scrapy.Item):
    title = scrapy.Field()
    salary = scrapy.Field()
    company = scrapy.Field()
    views = scrapy.Field()
    responses = scrapy.Field()
    pub_date = scrapy.Field()
    truncate_description = scrapy.Field()
    tags = scrapy.Field()
    url = scrapy.Field()
    category = scrapy.Field()
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
    direct_apply = scrapy.Field()
    raw_jsonld = scrapy.Field()
    raw_djinni_metadata = scrapy.Field()
    canonical_vacancy = scrapy.Field()

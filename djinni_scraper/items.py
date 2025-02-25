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

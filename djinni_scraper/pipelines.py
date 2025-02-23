# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://docs.scrapy.org/en/latest/topics/item-pipeline.html


# useful for handling different item types with a single interface
import sqlite3
import json
from itemadapter import ItemAdapter

from djinni_scraper.utils.telegram_utils import Telegram


class DjinniScraperPipeline:
    def process_item(self, item, spider):
        return item


class SQLitePipeline:
    DB_PATH = "jobs.db"

    def __init__(self):
        self.conn = None
        self.cursor = None

    def open_spider(self, spider):
        """ Підключення до бази при старті спайдера """
        try:
            spider.logger.info("📡 Opening SQLite pipeline.")
            self.conn = sqlite3.connect(self.DB_PATH)
            self.cursor = self.conn.cursor()

            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS vacancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT,
                    salary TEXT,
                    company TEXT,
                    views INTEGER,
                    responses INTEGER,
                    pub_date TEXT NOT NULL,
                    truncate_description TEXT,
                    tags TEXT,
                    category TEXT,
                    url TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(url, pub_date)
                )
            """)

            # Додаємо індекс для швидкого пошуку вакансій
            self.cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_url_pubdate ON vacancies (url, pub_date);
            """)

            self.conn.commit()
            spider.logger.info("✅ Table checked or created successfully.")

        except sqlite3.Error as e:
            spider.logger.error(f"❌ Error connecting to SQLite: {e}")
            raise

    def close_spider(self, spider):
        """ Закриваємо з'єднання з базою при завершенні роботи """
        try:
            spider.logger.info("Closing SQLite pipeline.")
            if self.cursor:
                self.cursor.close()
            if self.conn:
                self.conn.close()
                spider.logger.info("✅ Connection successfully closed.")
        except sqlite3.Error as e:
            spider.logger.error(f"❌ Error closing SQLite connection: {e}")

    def process_item(self, item, spider):
        """ Перевіряє та додає вакансію в базу """
        telegram = Telegram(spider)
        try:
            adapter = ItemAdapter(item)
            url = adapter.get('url')
            pub_date = adapter.get('pub_date')

            if not url or not pub_date:
                spider.logger.warning("⚠️ Item does not have a valid URL or pub_date. Skipping insert.")
                return item

            # Перевіряємо, чи існує вакансія з таким `url` і `pub_date`
            self.cursor.execute("""
                SELECT 1 FROM vacancies WHERE url = ? AND pub_date = ?
            """, (url, pub_date))

            if self.cursor.fetchone():
                spider.logger.info(f"🔄 Vacancy already exists: {url} - {pub_date}. Skipping insert.")
                return item

            tags = json.dumps(adapter.get('tags', []))

            # Додаємо вакансію
            data = {
                "title": adapter.get('title', 'No Title'),
                "salary": adapter.get('salary', 'Not specified'),
                "company": adapter.get('company', 'Unknown'),
                "views": adapter.get('views', 0),
                "responses": adapter.get('responses', 0),
                "pub_date": pub_date,
                "truncate_description": adapter.get('truncate_description', ''),
                "tags": tags,
                "url": url,
                "category": adapter.get('category'),
            }

            self.cursor.execute("""
                INSERT INTO vacancies (title, salary, company, views, responses, pub_date, 
                                       truncate_description, tags, category, url) 
                VALUES (:title, :salary, :company, :views, :responses, :pub_date, 
                        :truncate_description, :tags, :category, :url)
            """, data)
            telegram.send_job_to_telegram(data)
            self.conn.commit()
            spider.logger.info(f"✅ Vacancy saved: {url} - {pub_date}")

        except sqlite3.Error as e:
            spider.logger.error(f"❌ Database error while saving item: {e}")
            self.conn.rollback()

        except Exception as e:
            spider.logger.error(f"❌ Unexpected error in process_item: {e}")

        return item

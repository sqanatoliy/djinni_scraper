import os

BOT_NAME = "djinni_scraper"

SPIDER_MODULES = ["djinni_scraper.spiders"]
NEWSPIDER_MODULE = "djinni_scraper.spiders"

USER_AGENT = None

# === Playwright Settings ===
PLAYWRIGHT_BROWSER_TYPE = "chromium"
PLAYWRIGHT_LAUNCH_OPTIONS = {
    # "executable_path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "headless": True,
    "args": [
        "--disable-blink-features=AutomationControlled",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ],
}
PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT = 30_000
PLAYWRIGHT_CONTEXTS = {
    "default": {
        "viewport": {"width": 1920, "height": 1080},
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "java_script_enabled": True,
        "extra_http_headers": {
            "Accept-Language": "en-US,en;q=0.9,uk-UA;q=0.8,uk;q=0.7",
            "Referer": "https://djinni.co/",
        },
        # "storage_state": "state.json",
    }
}
if os.path.exists("state.json"):
    PLAYWRIGHT_CONTEXTS["default"]["storage_state"] = "state.json"


DOWNLOAD_HANDLERS = {
    "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
}

# === Pipelines ===
ITEM_PIPELINES = {
    'djinni_scraper.pipelines.SQLitePipeline': 300,
}

# Obey robots.txt rules
ROBOTSTXT_OBEY = False

# Set settings whose default value is deprecated to a future-proof value
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"

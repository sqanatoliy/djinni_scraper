import os
import logging
from logging.handlers import RotatingFileHandler
from scrapy.utils.log import configure_logging

BOT_NAME = "djinni_scraper"

SPIDER_MODULES = ["djinni_scraper.spiders"]
NEWSPIDER_MODULE = "djinni_scraper.spiders"

# === Scrapy Performance Settings ===
CONCURRENT_REQUESTS = 1
DOWNLOAD_DELAY = 1

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

# === Logging Settings ===
LOG_LEVEL = "INFO"

# === Logging directory settings ===
# LOGS_DIR = "logs"  # Directory for saving logs
# LOGS_FILE = os.path.join(LOGS_DIR, "djinni_scraper.log")  # Full path to the log file
# MAX_LOG_FILE_SIZE = 1 * 1024 * 1024 * 1024  # Maximum size of the log file (1 GB)
# BACKUP_COUNT = 5  # Number of backup copies of logs
#
# # Ensure logs directory exists
# os.makedirs(LOGS_DIR, exist_ok=True)

# Configure Scrapy Logging
configure_logging(install_root_handler=False)
logger = logging.getLogger()

# Remove existing FileHandlers to avoid duplicates
logger.handlers = [
    handler for handler in logger.handlers if not isinstance(handler, logging.FileHandler)
]

logger.setLevel(LOG_LEVEL)

# # Rotating File Handler (Logs to file with rotation)
# rotating_handler = RotatingFileHandler(
#     LOGS_FILE, maxBytes=MAX_LOG_FILE_SIZE, backupCount=BACKUP_COUNT
# )  # Creating a handler to rotate log files
# formatter = logging.Formatter(
#     "%(asctime)s [%(levelname)s] %(name)s [%(funcName)s]: %(message)s"
# )
# rotating_handler.setFormatter(formatter)
# rotating_handler.setLevel(logging.INFO)  # Setting the logging level (if different from general)
# logger.addHandler(rotating_handler)  # Adding a handler to the logger

# Console Handler (Logs to terminal)
if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s]: %(message)s"))
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

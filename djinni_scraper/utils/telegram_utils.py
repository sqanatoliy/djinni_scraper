import json
import time
from typing import Any, Dict

import requests
from decouple import config

# Telegram API
TELEGRAM_TOKEN = config("TELEGRAM_TOKEN")
CHAT_ID = config("CHAT_ID")


class Telegram:
    """Telegram API wrapper."""
    TELEGRAM_API_URL = "https://api.telegram.org/bot{}/sendMessage"

    def __init__(self, spider: Any) -> None:
        self.spider = spider

    @staticmethod
    def _clean_text_for_telegram(text: str) -> str:
        """Cleans text for Telegram compatibility."""
        return text.replace("`", "'").replace("’", "'").strip()

    def _create_telegram_message(self, data: dict) -> str:
        """Creates a formatted message for Telegram."""
        # Розкодовуємо JSON-теги в список
        raw_tags = data.get("tags", "[]")  # Значення з БД (рядок JSON)
        try:
            tags_list = json.loads(raw_tags)  # Конвертуємо в список
            tags_text = ", ".join(tags_list) if tags_list else "No tags"
        except json.JSONDecodeError:
            tags_text = raw_tags  # Якщо раптом це вже звичайний текст
        return (
            f"DJINNI.CO in category {data['category']} \n"
            f"*Date:* {data['pub_date']}\n"
            f"[{data['title']}]({data['url']}) at *{data['company']}*\n"
            f"*Views:* {data['views']}\n"
            f"*Responses:* {data['responses']}\n"
            f"*Salary:* {data['salary'] or 'N/A'}\n"
            f"*Tags:* {tags_text}\n"
            f"*Desc:* {self._clean_text_for_telegram(data['truncate_description'] or 'N/A')}"
        )

    def send_job_to_telegram(self, data: dict) -> None:
        """Sends a job offer to a Telegram chat."""
        msg: str = self._create_telegram_message(data)
        payload: Dict[str, Any] = {
            "chat_id": CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        while True:
            try:
                response: requests.Response = requests.post(
                    self.TELEGRAM_API_URL.format(TELEGRAM_TOKEN),
                    data=payload,
                    timeout=60,
                )
                if response.status_code == 429:
                    retry_time = self._get_retry_time(response)
                    self.spider.logger.warning(
                        "Telegram API rate limit exceeded. Waiting and retrying after %s seconds.",
                        retry_time
                    )
                    time.sleep(retry_time)
                    continue
                response.raise_for_status()
                self.spider.logger.info(f"Job sent to Telegram successfully at Djinni {data['category']} category.")
                break
            except requests.exceptions.HTTPError as err:
                self.spider.logger.error("HTTP error occurred: %s", err)
                time.sleep(10)
            except requests.exceptions.ConnectionError as err:
                self.spider.logger.error("Connection error occurred: %s", err)
                time.sleep(10)
            except requests.exceptions.Timeout as err:
                self.spider.logger.error("Timeout error occurred: %s", err)
                time.sleep(10)
            except requests.exceptions.RequestException as err:
                self.spider.logger.error("Failed to send job to Telegram: %s", err)
                time.sleep(10)

    @staticmethod
    def _get_retry_time(response: requests.Response) -> int:
        """Extracts retry time from the Telegram API response."""
        try:
            return int(response.json().get("parameters", {}).get("retry_after", 5))
        except (ValueError, KeyError):
            return 5

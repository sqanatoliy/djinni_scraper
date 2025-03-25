import json
import re
import time
from typing import Any, Dict

import requests
from decouple import config



class Telegram:
    """Telegram API wrapper."""
    TELEGRAM_API_URL = "https://api.telegram.org/bot{}/sendMessage"

    def __init__(self, spider: Any) -> None:
        self.spider = spider
        self.token = config("TELEGRAM_TOKEN", default="dummy_token")
        self.chat_id = config("CHAT_ID", default="dummy_chat_id")

    @staticmethod
    def _clean_text_for_telegram(text: str) -> str:
        """Escape text for Telegram MarkdownV2."""
        text = text.replace("`", "'").replace("’", "'").strip()
        # Telegram MarkdownV2 requires escaping all of the following characters:
        escape_chars = r'_*[]()~`>#+-=|{}.!\\'
        return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)

    def _create_telegram_message(self, data: dict) -> str:
        """Creates a formatted message for Telegram."""
        # Розкодовуємо JSON-теги в список
        raw_tags = data.get("tags", "[]")  # Значення з БД (рядок JSON)
        try:
            tags_list = json.loads(raw_tags)  # Конвертуємо в список
            tags_text = ", ".join(tags_list) if tags_list else "No tags"
        except json.JSONDecodeError:
            tags_text = raw_tags  # Якщо раптом це вже звичайний текст
        clean = self._clean_text_for_telegram
        return (
            f"{clean('DJINNI.CO in category')} {clean(data['category'])}\n"
            f"*{clean('Date:')}* {clean(data['pub_date'])}\n"
            f"[{clean(data['title'])}]({clean(data['url'])}) {clean('at')} *{clean(data['company'])}*\n"
            f"*{clean('Views:')}* {clean(str(data['views']))}\n"
            f"*{clean('Responses:')}* {clean(str(data['responses']))}\n"
            f"*{clean('Salary:')}* {clean(data['salary'] or 'N/A')}\n"
            f"*{clean('Tags:')}* {clean(tags_text)}\n"
            f"*{clean('Desc:')}* {clean(data['truncate_description'] or 'N/A')}"
        )

    def send_job_to_telegram(self, data: dict) -> None:
        """Sends a job offer to a Telegram chat."""
        msg: str = self._create_telegram_message(data)
        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": msg,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }

        while True:
            try:
                response: requests.Response = requests.post(
                    self.TELEGRAM_API_URL.format(self.token),
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

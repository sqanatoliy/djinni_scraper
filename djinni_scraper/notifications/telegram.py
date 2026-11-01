"""Canonical Vacancy MarkdownV2 formatting and one-attempt Telegram transport."""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import requests

from djinni_scraper.domain.vacancy import Compensation, Location, Vacancy
from djinni_scraper.notifications.routing import TelegramDestination


PARSE_MODE = "MarkdownV2"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_MARKDOWN_V2_CHARS = r"_*[]()~`>#+-=|{}.!\\"
_DESCRIPTION_LIMIT = 1_500
_FIELD_LIMIT = 500


class TelegramDeliveryError(RuntimeError):
    """A concise transport failure that is safe to persist in outbox diagnostics."""


def escape_markdown_v2(value: object) -> str:
    """Escapes source-controlled content for the configured Telegram parse mode."""
    text = str(value).replace("`", "'").replace("’", "'").strip()
    return re.sub(f"([{re.escape(_MARKDOWN_V2_CHARS)}])", r"\\\1", text)


def format_vacancy_notification(
    vacancy: Vacancy,
    *,
    event_type: str | None = None,
    publication_published_at: str | None = None,
) -> str:
    """Builds a compact MarkdownV2 message from canonical Vacancy plus event context."""
    lines = []
    if event_type == "VACANCY_REPUBLISHED":
        lines.append("🔄 *Повторно опублікована вакансія*")
    lines.extend(["*DJINNI\\.CO*", f"*{escape_markdown_v2(_clip(vacancy.title, _FIELD_LIMIT))}*"])
    _append_field(lines, "Компанія", _company_text(vacancy))
    _append_field(lines, "Винагорода", _compensation_text(vacancy.compensation))
    _append_field(lines, "Досвід", _experience_text(vacancy.experience_months))
    _append_field(lines, "Тип зайнятості", vacancy.employment_type)
    _append_field(lines, "Локація", _locations_text(vacancy.locations))
    _append_field(lines, "Доступно з", _locations_text(vacancy.applicant_locations))
    _append_field(lines, "Категорія", vacancy.category)
    _append_field(lines, "Індустрія", vacancy.industry)
    _append_field(lines, "Теги", ", ".join(vacancy.tags) if vacancy.tags else None)
    _append_field(lines, "Мови", _languages_text(vacancy))
    displayed_published_at = (
        publication_published_at
        if event_type == "VACANCY_REPUBLISHED" and publication_published_at is not None
        else vacancy.published_at
    )
    _append_field(lines, "Опубліковано", _date_text(displayed_published_at))
    if vacancy.description:
        _append_field(lines, "Опис", _description_text(vacancy.description))
    lines.append(f"[Відкрити вакансію]({_escape_link_url(vacancy.url)})")
    return "\n".join(lines)


class TelegramClient:
    """Sends one request per dispatcher attempt; retry scheduling belongs to the outbox."""

    def __init__(
        self,
        *,
        timeout_seconds: int,
        post: Callable[..., Any] = requests.post,
    ) -> None:
        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise ValueError("Telegram timeout must be a positive integer")
        self.timeout_seconds = timeout_seconds
        self._post = post

    def send(self, destination: TelegramDestination, message: str) -> None:
        """Performs exactly one Telegram HTTP attempt without exposing credentials."""
        try:
            response = self._post(
                TELEGRAM_API_URL.format(token=destination.token),
                data={
                    "chat_id": destination.chat_id,
                    "text": message,
                    "parse_mode": PARSE_MODE,
                    "disable_web_page_preview": True,
                },
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as error:
            raise TelegramDeliveryError(f"Telegram request failed: {type(error).__name__}") from error
        if not 200 <= response.status_code < 300:
            raise TelegramDeliveryError(f"Telegram returned HTTP {response.status_code}")


def _append_field(lines: list[str], label: str, value: str | None) -> None:
    if value:
        lines.append(f"*{escape_markdown_v2(label)}:* {escape_markdown_v2(_clip(value, _FIELD_LIMIT))}")


def _company_text(vacancy: Vacancy) -> str | None:
    return vacancy.company.name if vacancy.company and vacancy.company.name else None


def _compensation_text(compensation: Compensation | None) -> str | None:
    if compensation is None:
        return None
    currency = compensation.currency or ""
    if compensation.min_amount is not None and compensation.max_amount is not None:
        amount = f"{compensation.min_amount:g}–{compensation.max_amount:g}"
    elif compensation.min_amount is not None:
        amount = f"від {compensation.min_amount:g}"
    elif compensation.max_amount is not None:
        amount = f"до {compensation.max_amount:g}"
    else:
        return None
    unit = f" ({compensation.unit})" if compensation.unit else ""
    return " ".join(part for part in (amount, currency) if part) + unit


def _experience_text(value: int | float | None) -> str | None:
    if value is None:
        return None
    return f"{value:g} міс."


def _locations_text(locations: tuple[Location, ...]) -> str | None:
    values = []
    for location in locations:
        parts = [part for part in (location.city, location.region, location.country) if part]
        if parts:
            values.append(", ".join(parts))
    return "; ".join(values) if values else None


def _languages_text(vacancy: Vacancy) -> str | None:
    values = [
        f"{requirement.language} ({requirement.level})" if requirement.level else requirement.language
        for requirement in vacancy.language_requirements
    ]
    return ", ".join(values) if values else None


def _date_text(value: date | datetime | str | None) -> str | None:
    if isinstance(value, str):
        return value
    return value.isoformat() if value is not None else None


def _description_text(value: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", value))
    text = " ".join(text.split())
    return _clip(text, _DESCRIPTION_LIMIT)


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1].rstrip()}…"


def _escape_link_url(url: str) -> str:
    return url.replace("\\", "\\\\").replace(")", "\\)")

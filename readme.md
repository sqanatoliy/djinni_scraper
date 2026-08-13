# Djinni Scraper

Production-ready personal scraper вакансій Djinni. Він використовує Scrapy і Playwright для source-specific parsing, а нижче canonical boundary працює лише з `Vacancy`.

## Встановлення

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Скопіюйте `.env_template` у локальний `.env` і задайте Djinni credentials, SQLite path та потрібні Telegram routes. Файл `.env` не відстежується Git.

## Запуск формальних profiles

```bash
scrapy crawl djinni -a profile=python
scrapy crawl djinni -a profile=data_engineer
scrapy crawl djinni -a profile=other
scrapy crawl djinni -a profile=support
```

Формальні profiles централізовано визначають Djinni filters, discovery provenance і logical notification route.

## Ad-hoc запуск

```bash
scrapy crawl djinni \
  -a primary_keyword="Data Engineer" \
  -a exp_level="1y,2y" \
  -a english_level="no_english,basic,pre,intermediate" \
  -a employment=remote \
  -a category=data_engineer_remote
```

У ad-hoc режимі `category` не передається на Djinni: це локальний `discovery_profile`. Без явного route застосовується `main`.

## Архітектура

```text
Djinni HTML / JSON-LD
→ DjinniScraperItem (source DTO)
→ normalize_djinni_vacancy()
→ canonical Vacancy
→ SQLite identity / updates / discoveries
→ transactional outbox
→ Telegram
```

`DjinniScraperItem` зберігає тільки Djinni-specific transport data. `Vacancy` — незалежна від Scrapy, SQLite та Telegram domain model.

## SQLite і сповіщення

- Runtime DB: `runtime/vacancies.db` (schema version 3).
- Одна logical vacancy визначається через `source + source_vacancy_id`; без stable ID використовується normalized URL fallback.
- `vacancy_discoveries` зберігає profile provenance та timestamps першого/останнього виявлення.
- `notification_outbox` створюється транзакційно разом з новою vacancy.

Політика Telegram:

```text
INSERTED       → notification event
UPDATED        → без нового повідомлення
UNCHANGED      → без нового повідомлення
discovery-only → без нового повідомлення
```

Routes `main` і `alternate` отримують credentials лише з environment. Невдала delivery залишається pending із часом наступної спроби; токени й chat IDs не пишуться до SQLite або логів.

## GitHub Actions state

Scheduled workflow послідовно запускає чотири formal profiles і зберігає `runtime/vacancies.db` через GitHub Actions cache. Це best-effort state: cache може бути evicted, тоді нова DB не матиме попередньої dedup history.

`runtime/`, `state.json`, `.env`, SQLite backups, Python caches і `.DS_Store` не комітяться.

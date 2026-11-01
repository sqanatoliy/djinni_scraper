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

## Відновлення Djinni сесії

За відсутнього або stale `state.json` scraper залишає початкову Playwright-вкладку без login-взаємодії, через 2 секунди створює в тому самому context нову вкладку, ще через 2 секунди відкриває у ній головну Djinni. Потім він автоматично натискає локалізований `Увійти` / `Log In`, заповнює форму, чекає 2 секунди перед submit, перевіряє authenticated navigation marker і лише після цього зберігає новий storage state. Після завершення auth-flow обидві callback-owned сторінки закриваються, але context і Chromium лишаються під керуванням scrapy-playwright. Прямий перехід на `/login` не використовується як login entry point.

## SQLite і сповіщення

- Runtime DB: `runtime/vacancies.db` (schema version 4).
- Одна logical vacancy визначається через `source + source_vacancy_id`; без stable ID використовується normalized URL fallback.
- `vacancy_discoveries` зберігає profile provenance та timestamps першого/останнього виявлення.
- `vacancy_publications` зберігає history publication days, а `notification_outbox` транзакційно зберігає `INSERTED` і `REPUBLISHED` delivery state.

Політика Telegram:

```text
INSERTED       → notification event
REPUBLISHED     → notification event
UPDATED        → без нового повідомлення
UNCHANGED      → без нового повідомлення
discovery-only → без нового повідомлення
```

Routes `main` і `alternate` отримують credentials лише з environment. Невдала delivery залишається pending із часом наступної спроби; токени й chat IDs не пишуться до SQLite або логів. Успішні sends розділяються `TELEGRAM_SEND_INTERVAL_SECONDS` (default `1.0`). Для Telegram HTTP 429 dispatcher бере JSON `parameters.retry_after`, потім HTTP `Retry-After`, інакше `TELEGRAM_RETRY_DELAY_SECONDS`; фіксує лише поточний event та зупиняє ordered batch без нових attempts.

## GitHub Actions state

`runtime/vacancies.db` — локальний runtime file. У GitHub Actions authoritative copy зберігається в Cloudflare R2 як:

```text
s3://$R2_BUCKET_NAME/production/vacancies.db
```

Hourly workflow виконує `restore → crawl → validate → persist`. Він відновлює object у temporary file, перевіряє SQLite перед використанням, а після profiles перевіряє `quick_check`, `foreign_key_check`, schema version 4 і required tables перед upload. Якщо object відсутній, це explicit first-run bootstrap; інші R2 failures зупиняють workflow, не створюючи fresh DB. GitHub Actions cache більше не використовується для application DB state.

Для workflow потрібні лише names secrets: `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT_URL`, `R2_BUCKET_NAME`. Значення не зберігаються в repository або документації.

`runtime/`, `state.json`, `.env`, SQLite backups, Python caches і `.DS_Store` не комітяться.

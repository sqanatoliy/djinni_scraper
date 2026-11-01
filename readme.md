# Djinni Scraper

Production-ready personal scraper вакансій Djinni і DOU. Нижче source-specific parsing canonical boundary працює лише з `Vacancy`.

## Встановлення

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium  # потрібен лише для Djinni
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

DOU має окремі typed profiles і source-aware runner:

```bash
python scripts/run_dou_profile.py --profile dou-python-0-1
python scripts/run_dou_profile.py --profile dou-python-1-3
python scripts/run_dou_profile.py --profile dou-data-engineer-0-1
python scripts/run_dou_profile.py --profile dou-data-engineer-1-3
python scripts/run_dou_profile.py --profile dou-first-job-remote
python scripts/run_dou_profile.py --profile dou-support-remote-0-1
```

DOU runner працює через plain Scrapy HTTP: без Playwright, browser state, Djinni credentials або окремих Telegram secrets. Він вважає profile успішним лише після terminal listing/pagination contract і обробки всіх запитаних detail pages; валідний zero-result пошук також є успішним.

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
Djinni HTML / JSON-LD → DjinniScraperItem → normalize_djinni_vacancy()
DOU public HTML / XHR → DouScraperItem → normalize_dou_vacancy()
→ canonical Vacancy
→ SQLite identity / updates / discoveries
→ transactional outbox
→ Telegram
```

Source DTO зберігають лише source-specific transport evidence. `Vacancy` — незалежна від Scrapy, SQLite та Telegram domain model. DOU identity — `source="dou"` разом з numeric segment після `/vacancies/`; query, fragment і trailing slash не впливають на canonical URL.

## DOU transport contract

Standard vacancy listings use narrowed selector `#vacancyListId ul.lt > li.l-vacancy`. Їхня DOU pagination зберігає Scrapy cookie jar: listing page exposes `window.CSRF_TOKEN` and `window.XHR_VS_URL`, then `FormRequest` sends `csrfmiddlewaretoken` and actual parsed-card `count`. JSON response is validated as `html`, `num`, `last`; `count` advances by `num`, and a non-final zero-progress response fails the profile. `first-job` має окремий terminal HTML contract `div.first-job-vacancies > ul > li.l-vacancy`: він не виконує XHR і не вимагає CSRF.

Detail HTML is required for a complete DOU observation: title and description container must be present. Salary and explicit cities are parsed conservatively; remote markers never become a city, and search `exp` is discovery evidence rather than actual `experience_months`. Existing SQLite schema v5 creates versions, publications, discoveries and outbox events automatically for canonical DOU observations.

## Відновлення Djinni сесії

За відсутнього або stale `state.json` scraper залишає початкову Playwright-вкладку без login-взаємодії, через 2 секунди створює в тому самому context нову вкладку, ще через 2 секунди відкриває у ній головну Djinni. Потім він автоматично натискає локалізований `Увійти` / `Log In`, заповнює форму, чекає 2 секунди перед submit, перевіряє authenticated navigation marker і лише після цього зберігає новий storage state. Після завершення auth-flow обидві callback-owned сторінки закриваються, але context і Chromium лишаються під керуванням scrapy-playwright. Прямий перехід на `/login` не використовується як login entry point.

## SQLite і сповіщення

- Runtime DB: `runtime/vacancies.db` (schema version 5).
- Одна logical vacancy визначається через `source + source_vacancy_id`; без stable ID використовується normalized URL fallback.
- `vacancy_discoveries` зберігає profile provenance та timestamps першого/останнього виявлення.
- `vacancy_publications` зберігає history publication days, а `notification_outbox` транзакційно зберігає `INSERTED` і `REPUBLISHED` delivery state.
- `vacancy_versions` зберігає незмінний JSON snapshot кожного distinct meaningful canonical content state. Незмінні щогодинні observations і republish лише з новою датою не створюють version; incomplete first observation має marker `snapshot_complete`, а повне збагачення створює окремий complete state.

Архітектурний контракт для майбутньої аналітики: `sources → canonical Vacancy → vacancies + vacancy_versions → source-neutral enrichment`. Кожен meaningful canonical content state можна відновити з history без повторного scraping; майбутні skill/technology taxonomy, mentions і trends будуватимуться над цими snapshots. Operational identity лишається source-qualified, тому Djinni, DOU та наступні sources не merge-яться. `vacancy_discoveries` лишається окремим provenance history, який можна join-ити з versions. Поточні дані є targeted sample за активними профілями, а не повним зрізом ринку.

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

Hourly workflow `Run Job Scrapers Hourly` виконує `restore → Djinni profiles → six DOU profiles → validate → persist` в одному job та з одним local SQLite file. Він відновлює object у temporary file, перевіряє SQLite перед використанням, а після profiles перевіряє `quick_check`, `foreign_key_check`, schema version 5 і required tables перед upload. Якщо object відсутній, це explicit first-run bootstrap; інші R2 failures зупиняють workflow, не створюючи fresh DB. GitHub Actions cache більше не використовується для application DB state; failure будь-якого profile робить workflow failed, хоча валідний partial DB може бути safely uploaded за чинною mutation policy.

Для workflow потрібні лише names secrets: `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT_URL`, `R2_BUCKET_NAME`. Значення не зберігаються в repository або документації.

`runtime/`, `state.json`, `.env`, SQLite backups, Python caches і `.DS_Store` не комітяться.

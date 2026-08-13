# Djinni Scraper

Проєкт перебуває в активній розробці. Цей веб-скрейпер використовує **Scrapy** у поєднанні з **Playwright**, щоб автоматизовано отримувати дані з сайту [Djinni](https://djinni.co/).

## 📌 Мета проєкту

Скрапер буде збирати інформацію про:
- Вакансії (назва, компанія, рівень, локація, вимоги тощо)
- Зарплатні діапазони
- Тренди ринку праці

Зібрані дані можуть бути використані для аналітики ринку IT, побудови звітів та інших Data Engineering завдань.

## 🛠 Технології

- **Python** 3.10+
- **Scrapy** + **Scrapy-Playwright**
- **Playwright** (для роботи з динамічним контентом)
- **Docker** (опціонально, для зручного розгортання)
- **SQLite / PostgreSQL** (для збереження даних)
- **GitHub Actions** (для автоматизованого запуску скрапінгу)
- **Telegram API** (для надсилання повідомлень у групу)

## 🚀 Встановлення та запуск

### 1️⃣ Клонування репозиторію
```bash
git clone https://github.com/sqanatoliy/djinni-scraper.git
cd djinni-scraper
```

### 2️⃣ Створення віртуального середовища
```bash
python -m venv venv
source venv/bin/activate  # для Linux/Mac
venv\Scripts\activate  # для Windows
```

### 3️⃣ Встановлення залежностей
```bash
pip install -r requirements.txt
```

### 4️⃣ Налаштування Playwright
```bash
playwright install
playwright install-deps  # для Linux
```

### 5️⃣ Запуск скрапінгу
Основний інтерфейс — формальний profile, який централізовано визначає Djinni filters:

```bash
scrapy crawl djinni -a profile=python -o jobs.json
```

Доступні profiles: `python`, `data_engineer`, `other`, `support`.

Для ad-hoc пошуку збережено legacy arguments. `category` у цьому режимі — discovery slug, а не source vacancy category:

```bash
scrapy crawl djinni \
  -a primary_keyword="Data Engineer" \
  -a employment=remote \
  -a category=data_engineer_remote \
  -o jobs.json
```

## 📁 Структура проєкту
```
.
.
├── djinni_scraper
│   ├── __init__.py
│   ├── items.py
│   ├── middlewares.py
│   ├── notifications
│   │   ├── dispatcher.py
│   │   ├── routing.py
│   │   └── telegram.py
│   ├── pipelines.py
│   ├── settings.py
│   ├── spiders
│   │   ├── __init__.py
│   │   └── djinni.py
│   └── utils
│       ├── __init__.py
│       └── url_utils.py
├── runtime/vacancies.db - runtime SQLite state (не version-controlled)
├── readme.md
├── requirements.txt
├── scrapy.cfg
├── state.json - авторизований стан Playwright
├── .github/workflows/scraper.yml  # GitHub Actions для автоматичного запуску
├── .env_template  # Файл із змінними середовища для налаштування Telegram, Djinni
```

## 🕒 Автоматизований запуск
- Скрапер автоматично запускається **щогодини** через **GitHub Actions**.
- Canonical вакансії зберігаються у SQLite за шляхом `runtime/vacancies.db` або значенням змінної середовища `DB_PATH`.
- Schema v3 зберігає одну logical vacancy за `source + source_vacancy_id`; за відсутнього ID застосовується fallback `source + normalized canonical URL`. Fallback row автоматично промотується, коли пізніше з'являється stable ID.
- Search-profile provenance зберігається нормалізовано в `vacancy_discoveries`. Повторне спостереження оновлює `last_seen_at`/`last_discovered_at`; зміна canonical content оновлює current snapshot, а не створює duplicate row.
- Restored schema v1/v2 cache автоматично мігрує до schema v3 без видалення canonical rows. Нові БД одразу ініціалізуються як v3; historical rows зі старого `jobs.db` навмисно не мігруються.
- Telegram використовує canonical `Vacancy` і transactional SQLite outbox: тільки `INSERTED` створює event. `UPDATED`, unchanged і discovery-only observations не створюють нових повідомлень.
- Logical routes `main` і `alternate` беруть destination із наявних Telegram secrets. Event фіксує route першого discovery; failure зберігає pending state та retry time, тому доставка може продовжитися в наступному workflow run.
- Runtime SQLite state не комітиться в Git. GitHub Actions використовує cache як best-effort тимчасове збереження між scheduled runs.
- Cache може бути evicted; у такому разі наступний run створить fresh DB і раніше побачені вакансії можуть знову вважатися новими.
- Якщо налаштовані змінні середовища у `.env_template`, нові вакансії надсилаються у Telegram-групу.

---
🔗 **Контакти**: Якщо маєте питання чи пропозиції, пишіть у Issues або Pull Requests!

📝 **Ліцензія**: [MIT](./LICENSE)

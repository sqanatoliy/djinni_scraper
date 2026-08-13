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
│   ├── pipelines.py
│   ├── settings.py
│   ├── spiders
│   │   ├── __init__.py
│   │   └── djinni.py
│   └── utils
│       ├── __init__.py
│       ├── db_utls.py
│       ├── telegram_utils.py
│       └── url_utils.py
├── jobs.json - збережені вакансії в форматі JSON
├── readme.md
├── requirements.txt
├── scrapy.cfg
├── state.json - авторизований стан Playwright
├── .github/workflows/scraper.yml  # GitHub Actions для автоматичного запуску
├── .env_template  # Файл із змінними середовища для налаштування Telegram, Djinni
└── jobs.db  # Локальна база даних SQLite
```

## 🕒 Автоматизований запуск
- Скрапер автоматично запускається **щогодини** через **GitHub Actions**.
- Зібрані вакансії зберігаються у **SQLite** базу даних (`database.sqlite`).
- Якщо налаштовані змінні середовища у `.env_template`, нові вакансії надсилаються у Telegram-групу.

---
🔗 **Контакти**: Якщо маєте питання чи пропозиції, пишіть у Issues або Pull Requests!

📝 **Ліцензія**: [MIT](./LICENSE)

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
    Фільтри можна задати через аргументи командного рядка.
    Наприклад, для пошуку вакансій Python розробника додаємо такі аргументи: -a primary_keyword=Python
    Для пеоедачі множинних аргументів використовуємо кому.
    Наприклад -a exp_level=no_exp,1y,2y,3y
    Для збереження результатів використовуємо ключ -o jobs.json
    Команда для пошуку вакансій Python розробника з досвідом 1-3 роки та рівнем англійської мови до intermediate включно:

```bash
scrapy crawl djinni -a primary_keyword=Python -a exp_level=no_exp,1y,2y,3y -a english_level=no_english,basic,pre,intermediate -o jobs.json
```

## 📁 Структура проєкту
```
.
├── djinni_scraper
│   ├── __init__.py
│   ├── items.py
│   ├── middlewares.py
│   ├── pipelines.py
│   ├── settings.py
│   ├── spiders
│   │   ├── __init__.py
│   │   └── djinni.py - Основний скрапер
│   └── utils.py - Генерує стартовий URL з аргументів командного рядка
├── jobs.json - Результати скрапінгу в форматі JSON
├── readme.md
├── requirements.txt
├── scrapy.cfg
└── state.json - Зберігає авторизований стан скрапера
```

## 📌 Подальші плани
✅ Отримання та збереження вакансій
✅ Обхід CAPTCHA та авторизація (якщо буде потрібно)
🔜 Збереження даних у БД
🔜 Аналітика трендів

---
🔗 **Контакти**: Якщо маєте питання чи пропозиції, пишіть у Issues або Pull Requests!

📝 **Ліцензія**: [MIT](./LICENSE)
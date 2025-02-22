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
git clone https://github.com/your-repo/djinni-scraper.git
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
```bash
scrapy crawl djinni
```

## 📁 Структура проєкту
```
📂 djinni_scraper
├── 📂 spiders        # Павуки для Scrapy
├── 📂 middlewares    # Middleware для Scrapy
├── 📂 pipelines      # Обробка та збереження даних
├── scrapy.cfg        # Конфігурація Scrapy
└── requirements.txt  # Залежності
```

## 📌 Подальші плани
✅ Отримання та збереження вакансій
✅ Обхід CAPTCHA та авторизація (якщо буде потрібно)
🔜 Збереження даних у БД
🔜 Аналітика трендів

---
🔗 **Контакти**: Якщо маєте питання чи пропозиції, пишіть у Issues або Pull Requests!

📝 **Ліцензія**: [MIT](./LICENSE)
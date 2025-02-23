import os
import sqlite3
import json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(BASE_DIR, "jobs.db")


def check_database():
    """ Виводить список таблиць у базі даних """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()

        if not tables:
            print("⚠️ У базі даних немає жодної таблиці.")
        else:
            print("📌 Список таблиць у базі даних:")
            for table in tables:
                print(f" - {table[0]}")

    except sqlite3.Error as e:
        print(f"❌ Помилка при роботі з SQLite: {e}")
    finally:
        if conn:
            conn.close()


def fetch_tags():
    """Підключається до SQLite, отримує та друкує всі значення зі стовпця 'tags'"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vacancies';")
        if not cursor.fetchone():
            print("❌ Таблиця 'vacancies' не знайдена в базі даних.")
            return

        # Отримуємо всі унікальні значення 'tags'
        cursor.execute("SELECT tags FROM vacancies")
        rows = cursor.fetchall()

        print("📌 Список тегів:")
        for row in rows:
            tags = row[0]
            if tags:
                try:
                    # Якщо теги збережені у форматі JSON, розпарсимо їх
                    parsed_tags = json.loads(tags)
                    if isinstance(parsed_tags, list):
                        print(", ".join(parsed_tags))  # Вивід списком
                    else:
                        print(parsed_tags)  # Вивід як є
                except json.JSONDecodeError:
                    print(tags)  # Якщо це не JSON, виводимо як є
            else:
                print("⚠️ Порожнє значення")

    except sqlite3.Error as e:
        print(f"❌ Помилка при роботі з SQLite: {e}")
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    # check_database()
    fetch_tags()


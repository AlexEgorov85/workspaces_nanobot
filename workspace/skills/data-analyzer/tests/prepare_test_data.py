#!/usr/bin/env python3
"""Генерация тестовых файлов для проверки навыка."""
import os
import csv
import random
from datetime import datetime, timedelta

TEST_DIR = os.path.join(os.path.dirname(__file__), "test_files")
os.makedirs(TEST_DIR, exist_ok=True)


def create_small_text():
    path = os.path.join(TEST_DIR, "small.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("""Системный лог приложения.
2024-01-15 10:00:00 [INFO] Приложение запущено.
2024-01-15 10:01:23 [ERROR] Не удалось подключиться к базе данных.
2024-01-15 10:02:45 [WARN] Повторная попытка подключения.
2024-01-15 10:03:12 [INFO] Подключение успешно установлено.
2024-01-15 10:15:00 [ERROR] Таймаут запроса к API.
""")
    print(f"OK: {path}")


def create_large_log():
    path = os.path.join(TEST_DIR, "large.log")
    levels = ["INFO", "WARN", "ERROR", "DEBUG"]
    messages = [
        "Запрос обработан", "Пользователь авторизован", "Кэш обновлен",
        "Ошибка подключения", "Таймаут ответа", "Неверный токен",
        "Файл загружен", "Сессия завершена", "База данных синхронизирована",
    ]
    with open(path, "w", encoding="utf-8") as f:
        base = datetime(2024, 1, 15, 8, 0, 0)
        for i in range(2000):
            ts = base + timedelta(seconds=i * 3)
            level = random.choice(levels)
            msg = random.choice(messages)
            f.write(f"{ts.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg} (id={i})\n")
    print(f"OK: {path} (2000 lines)")


def create_csv_with_duplicates():
    path = os.path.join(TEST_DIR, "sales.csv")
    rows = [
        {"id": 1, "name": "Alice", "amount": 100, "date": "2024-01-01"},
        {"id": 2, "name": "Bob", "amount": 200, "date": "2024-01-02"},
        {"id": 1, "name": "Alice", "amount": 150, "date": "2024-01-03"},
        {"id": 3, "name": "Charlie", "amount": 300, "date": "2024-01-04"},
        {"id": 2, "name": "Bob", "amount": 250, "date": "2024-01-05"},
        {"id": 4, "name": "Diana", "amount": 400, "date": "2024-01-06"},
        {"id": 1, "name": "Alice", "amount": 120, "date": "2024-01-07"},
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "name", "amount", "date"])
        w.writeheader()
        w.writerows(rows)
    print(f"OK: {path} (7 rows, 3 duplicate ids)")


def create_complex_table():
    """Excel-файл для проверки агрегаций."""
    path = os.path.join(TEST_DIR, "analytics.xlsx")
    try:
        import pandas as pd
        df = pd.DataFrame({
            "category": ["A", "B", "A", "C", "B", "A", "C", "C"] * 10,
            "value": [random.randint(10, 100) for _ in range(80)],
            "region": ["North", "South"] * 40,
            "month": ["Jan", "Feb", "Mar", "Apr"] * 20,
        })
        df.to_excel(path, index=False)
        print(f"OK: {path} (80 rows)")
    except ImportError:
        print("SKIP: openpyxl not installed")


def main():
    print("Generating test data...")
    create_small_text()
    create_large_log()
    create_csv_with_duplicates()
    create_complex_table()
    print("Done.")


if __name__ == "__main__":
    main()

import duckdb

# Подключаемся к базе данных
conn = duckdb.connect('audit_cache.duckdb')

# Выполняем запрос на поиск последней закрытой проверки
result = conn.execute(
    'SELECT * FROM audits WHERE status = \"closed\" ORDER BY actual_date DESC LIMIT 1'
).fetchdf()

# Выводим результат
if not result.empty:
    row = result.iloc[0]
    print("Последняя закрытая проверка:")
    print(f"ID: {row['id']}")
    print(f"Название: {row['title']}")
    print(f"Тип проверки: {row['audit_type']}")
    print(f"Объект проверки: {row['auditee_entity']}")
    print(f"Дата завершения: {row['actual_date']}")
    print(f"Статус: {row['status']}")
else:
    print("Нет закрытых проверок в базе.")
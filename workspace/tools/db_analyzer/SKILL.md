# db_analyzer — Анализ аудиторских проверок

Инструмент `audit_analyze` с тремя режимами: `predefined`, `vector`, `sql`.

---

## Режимы работы

### 1. `predefined` — заготовленные скрипты

Выбор скрипта по имени (параметр `script`). Параметры передаются через `params`.

| Скрипт | Описание | Параметры |
|---|---|---|
| `analytics_by_year_month` | Аналитика по годам и месяцам | `year` (number) — год |
| `violations_by_type` | Статистика нарушений по кодам | `date_from` (date), `violation_code` (string, LIKE) |
| `top_audited_objects` | Топ проверяемых объектов | `limit` (number), `auditee_entity` (string, LIKE), `date_from` (date) |
| `audit_effectiveness` | Оценка эффективности проверок | `date_from` (date), `date_to` (date), `min_violations` (number, HAVING) |
| `audit_dynamics` | Динамика по периодам | `period` (enum: month/quarter/week), `date_from` (date) |
| `audit_types_stats` | Статистика по типам проверок | `audit_type` (string, LIKE), `date_from` (date) |

Параметры передаются строго по именам из таблицы. Типы:
- `number` — целое число (год, limit, min_violations)
- `date` — строка `YYYY-MM-DD`
- `like` — строка, автоматически оборачивается в `%...%`
- `enum` — строка из допустимых значений (month/quarter/week)

Пример вызова:
```json
{
  "mode": "predefined",
  "script": "top_audited_objects",
  "params": {"limit": 10, "auditee_entity": "университет"}
}
```

SQL-шаблоны используют:
- `{% if param %} ... {% endif %}` — условное включение фрагментов
- `:param` → `$1, $2` — безопасная подстановка через asyncpg

---

### 2. `sql` — генерация SQL через LLM

Pipeline генерации и выполнения:

```
Запрос → Получить схему БД → LLM генерирует SELECT
  → validate_sql (безопасность: запрет DDL/DML, мульти-запросов)
  → EXPLAIN (FORMAT JSON) — проверка синтаксиса и существования объектов
  → Если EXPLAIN упал: LLM получает ошибку PostgreSQL + запрос → исправляет
  → execute_query → результат
```

Цикл retry: до 3 попыток (1 генерация + 2 исправления).

Что проверяет EXPLAIN:
- Синтаксис SQL
- Существование таблиц и колонок
- Корректность JOIN и типов

Пример:
```json
{
  "mode": "sql",
  "query": "покажи топ 10 объектов по количеству нарушений за 2024 год"
}
```

---

### 3. `vector` — семантический поиск по FAISS

Поиск похожих документов по векторному индексу.

| Параметр | Описание |
|---|---|
| `query` | Текстовый запрос (обязательно) |
| `index_name` | Имя индекса без `.faiss` (обязательно) |
| `top_k` | Количество результатов (по умолчанию 5) |
| `threshold` | Порог схожести 0.0–1.0 (если задан, top_k игнорируется) |

Пример:
```json
{
  "mode": "vector",
  "query": "финансовые нарушения в университетах",
  "index_name": "audit_index",
  "top_k": 3
}
```

Эмбеддинги загружаются через HTTP к embedding-ендпоинту из конфига. Индексы — FAISS, загружаются из директории, указанной в настройках.

---

## Обработка ошибок

- **predefined**: если скрипт не найден — возвращается список доступных
- **sql**: EXPLAIN-валидация, retry до 3 раз, иначе ошибка с последним EXPLAIN-диагнозом
- **vector**: если индекс/зависимости не найдены — понятное сообщение об ошибке

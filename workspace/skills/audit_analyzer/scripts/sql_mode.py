"""
Режим: sql — LLM генерирует SELECT по описанию на естественном языке.

Pipeline с ретраями:
  1. Получить схему БД (information_schema)
  2. LLM генерирует SQL по схеме + запросу пользователя
  3. Валидация безопасности (только SELECT, один statement)
  4. EXPLAIN (FORMAT JSON) — проверка синтаксиса без выполнения
  5. Выполнить SELECT
  6. Если EXPLAIN или валидация упали — retry до MAX_RETRIES раз
     с передачей предыдущей ошибки в LLM для исправления

Пример запуска через CLI:
    audit_analyze --mode sql --query 'сколько аудитов было в 2024 по месяцам'
    audit_analyze --mode sql --query 'топ-10 объектов по количеству нарушений'
    audit_analyze --mode sql --query 'среднее количество нарушений на проверку'
"""


import re


from llm import chat
from skill_config import get_db_schema, get_db_tables

from lib.utils.sql_safety import format_schema, validate_sql

MAX_RETRIES = 2


def sanitize_sql_response(text: str) -> str:
    """Извлечь SQL из ответа LLM (CoT + markdown-обёртки).

    Для reasoning-моделей ответ часто выглядит так::

        <think>...</think>

        ```sql
        SELECT 1
        ```

    Или просто `` ```sql ... `` без мыслей. Здесь мы вытаскиваем первый
    SQL-запрос — либо из последнего `` ``` `` блока, либо по регулярному
    выражению ``SELECT|WITH|EXPLAIN``.
    """
    cleaned = text.strip()

    # Проверяем есть ли markdown-блок (```sql или просто ```)
    if "```" in cleaned:
        blocks = re.findall(r"```(?:sql)?\s*\n(.*?)```", cleaned, re.DOTALL)
        if blocks:
            return blocks[-1].strip().rstrip(";")

    # Пробуем вырезать мысли (</think> / ```xml-think ... ``` / think:)
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()
    cleaned = re.sub(r"```xml-think\s*\n.*?```", "", cleaned, flags=re.DOTALL).strip()
    cleaned = re.sub(r"^[^\S\n]*think:[^\n]*\n", "", cleaned, flags=re.MULTILINE).strip()

    # Если после очистки осталось только текст без SQL — вернём пустую строку
    if not re.search(r"\b(SELECT|WITH|EXPLAIN)\b", cleaned, re.IGNORECASE):
        return cleaned.strip().rstrip(";")

    return cleaned.strip().rstrip(";")


def run(query: str, db, context: list[dict] | None = None) -> dict:
    """
    Сгенерировать SQL через LLM, проверить, выполнить (с retry-циклом).

    Если LLM вернула некорректный SQL (не прошёл EXPLAIN или валидацию),
    ошибка передаётся обратно в LLM для исправления. До MAX_RETRIES + 1 попыток.

    Args:
        query: Запрос на естественном языке (например,
               'сколько проверок было в 2024 году по каждому объекту').
        db: Бэкенд запросов с методами query_sql(), explain(), get_schema().
        context: История чата (опционально — список сообщений).

    Returns:
        dict с полями:
            mode: "sql"
            status: "success" | "error"
            data:
                sql: сгенерированный SQL
                result: результат выполнения (columns, rows, row_count)
            (при ошибке) message: описание ошибки
    """
    tables = get_db_tables() or None
    schema = db.get_schema(schema_name=get_db_schema(), table_names=tables)
    schema_text = format_schema(schema)

    base_messages = [
        {
            "role": "system",
            "content": (
                "You are a PostgreSQL expert. Return ONLY a safe SELECT query. "
                "No explanations, no markdown, no SQL wrapping. Just the SQL."
            ),
        },
        {"role": "user", "content": f"Schema:\n{schema_text}\n\nRequest: {query}"},
    ]

    last_error: dict | None = None

    for attempt in range(MAX_RETRIES + 1):
        messages = list(base_messages)

        if attempt > 0 and last_error:
            messages.append({"role": "assistant", "content": last_error["sql"]})
            messages.append({
                "role": "user",
                "content": (
                    f"Предыдущий SQL-запрос вызвал ошибку: {last_error['error']}. "
                    f"Исправь запрос и верни только корректный SQL."
                ),
            })

        try:
            sql = chat(messages, context=context)
        except Exception as e:
            last_error = {"error": f"LLM call failed: {e}", "sql": ""}
            continue

        sql = sanitize_sql_response(sql)

        # Шаг 1: безопасность (DDL/DML/multi-statement)
        safety_error = validate_sql(sql)
        if safety_error:
            last_error = {"error": safety_error, "sql": sql}
            continue

        # Шаг 2: EXPLAIN — проверка синтаксиса и существования объектов
        explain_result = db.explain(sql)
        if not explain_result["valid"]:
            last_error = {"error": explain_result["error"], "sql": sql}
            if "временно занята" in explain_result.get("error", ""):
                break
            continue

        # Шаг 3: выполнить
        result = db.query_sql(sql)
        if result["status"] == "error" and "временно занята" in result.get("error", ""):
            last_error = {"error": result["error"], "sql": sql}
            break

        return {
            "mode": "sql",
            "status": result["status"],
            "data": {"sql": sql, "result": result},
        }

    # Все попытки исчерпаны
    detail = last_error or {"error": "неизвестная ошибка", "sql": ""}
    return {
        "status": "error",
        "data": {
            "message": (
                f"Не удалось сгенерировать корректный SQL после "
                f"{MAX_RETRIES + 1} попыток. Последняя ошибка: {detail['error']}"
            ),
            "sql": detail.get("sql", ""),
        },
    }

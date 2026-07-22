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

from typing import Optional

from skill_config import get_db_schema, get_db_tables
from database import Database
from llm import chat


MAX_RETRIES = 2


def run(query: str, db: Database, context: Optional[list[dict]] = None) -> dict:
    """
    Сгенерировать SQL через LLM, проверить, выполнить (с retry-циклом).

    Если LLM вернула некорректный SQL (не прошёл EXPLAIN или валидацию),
    ошибка передаётся обратно в LLM для исправления. До MAX_RETRIES + 1 попыток.

    Args:
        query: Запрос на естественном языке (например,
               'сколько проверок было в 2024 году по каждому объекту').
        db: Объект Database.
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
    schema_text = Database.format_schema(schema)

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

    last_error: Optional[dict] = None

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

        sql = sql.strip().rstrip(";")

        # Шаг 1: безопасность (DDL/DML/multi-statement)
        safety_error = Database.validate_sql(sql)
        if safety_error:
            last_error = {"error": safety_error, "sql": sql}
            continue

        # Шаг 2: EXPLAIN — проверка синтаксиса и существования объектов
        explain_result = db.execute_explain(sql)
        if not explain_result["valid"]:
            last_error = {"error": explain_result["error"], "sql": sql}
            if "временно занята" in explain_result.get("error", ""):
                break
            continue

        # Шаг 3: выполнить
        result = db.execute_query(sql)
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

"""Режим: sql — LLM генерирует SELECT по описанию на естественном языке.

Pipeline:
  1. Получить схему БД
  2. LLM генерирует SQL
  3. Валидация безопасности (validate_sql)
  4. EXPLAIN (FORMAT JSON) — проверка, что запрос корректен без выполнения
  5. Выполнить SELECT
  6. Если EXPLAIN упал — retry до max_retries раз с передачей ошибки в LLM
"""

from typing import Optional

from .config import get_db_schema
from .llm import chat
from .database import get_schema, execute_query, execute_explain, format_schema, validate_sql


MAX_RETRIES = 2


async def run(query: str, db_cfg: dict, context: Optional[list[dict]] = None) -> dict:
    """Сгенерировать, проверить и выполнить SQL.

    Args:
        query: Запрос на естественном языке.
        db_cfg: Конфигурация подключения к БД.
        context: Опциональный контекст чата (история сообщений).
    """
    schema = await get_schema(db_cfg, schema_name=get_db_schema())
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
            sql = await chat(messages, context=context)
        except Exception as e:
            last_error = {"error": f"LLM call failed: {e}", "sql": ""}
            continue

        sql = sql.strip().rstrip(";")

        # Шаг 1: безопасность (DDL/DML/multi-statement)
        safety_error = validate_sql(sql)
        if safety_error:
            last_error = {"error": safety_error, "sql": sql}
            continue

        # Шаг 2: EXPLAIN — проверка синтаксиса и существования объектов
        explain_result = await execute_explain(db_cfg, sql)
        if not explain_result["valid"]:
            last_error = {"error": explain_result["error"], "sql": sql}
            continue

        # Шаг 3: выполнить
        result = await execute_query(db_cfg, sql)
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

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

from config import get_db_schema
from llm import chat
from database import get_schema, execute_query, execute_explain, format_schema, validate_sql


MAX_RETRIES = 2


async def run(query: str, db_cfg: dict, context: Optional[list[dict]] = None) -> dict:
    """
    Сгенерировать SQL через LLM, проверить, выполнить (с retry-циклом).

    Если LLM вернула некорректный SQL (не прошёл EXPLAIN или валидацию),
    ошибка передаётся обратно в LLM для исправления. До MAX_RETRIES + 1 попыток.

    Args:
        query: Запрос на естественном языке (например,
               'сколько проверок было в 2024 году по каждому объекту').
        db_cfg: Конфигурация подключения к БД.
        context: История чата (опционально — список сообщений).

    Returns:
        dict с полями:
            mode: "sql"
            status: "success" | "error"
            data:
                sql: сгенерированный SQL
                result: результат выполнения (columns, rows, row_count)
            (при ошибке) message: описание ошибки

    Пример успеха:
        >>> import asyncio
        >>> from config import load_db_config
        >>> db = load_db_config()
        >>> asyncio.run(run("покажи всех нарушителей", db))  # doctest: +SKIP
        {'mode': 'sql', 'status': 'success', 'data': {'sql': 'SELECT ...', 'result': {...}}}

    Пример c контекстом (история чата):
        >>> history = [{"role": "user", "content": "Привет"}]
        >>> asyncio.run(run("сколько аудитов", db, context=history))  # doctest: +SKIP
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

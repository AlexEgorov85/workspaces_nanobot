"""Режим: generated_sql — LLM генерирует SELECT по описанию на естественном языке.

Pipeline с ретраями:
  1. Получить схему БД (information_schema через ``DuckDBService.get_schema``);
  2. LLM генерирует SQL по схеме + запросу пользователя (``lib.services.llm_client``);
  3. Валидация безопасности (только SELECT, один statement — ``validate_sql``);
  4. EXPLAIN — проверка синтаксиса без выполнения;
  5. Выполнить SELECT;
  6. Если EXPLAIN или валидация упали — retry до ``MAX_RETRIES`` раз
     с передачей предыдущей ошибки в LLM для исправления.
"""

from __future__ import annotations

from typing import Any

from llm import chat
from skill_config import get_db_schema, get_db_tables

from lib.utils.sql_safety import format_schema, validate_sql


__all__ = ["run", "MAX_RETRIES"]


MAX_RETRIES = 2


def run(query: str, db: Any, context: list[dict] | None = None) -> dict:
    """Сгенерировать SQL через LLM, проверить, выполнить (с retry-циклом).

    Если LLM вернула некорректный SQL (не прошёл EXPLAIN или валидацию),
    ошибка передаётся обратно в LLM для исправления. До ``MAX_RETRIES + 1``
    попыток (по умолчанию — 3).

    Args:
        query: Запрос на естественном языке (например,
            «сколько аудитов в 2024 по месяцам»).
        db: Бэкенд запросов с методами ``query_sql``, ``explain``,
            ``get_schema`` (см. ``DuckDBServiceProtocol``).
        context: История чата (опционально).

    Returns:
        ``{"mode": "generated_sql", "status": "success" | "error", "data": {...}}``.
        ``data.sql`` — сгенерированный SQL;
        ``data.result`` — результат выполнения;
        ``data.message`` — описание ошибки при ``status == "error"``.
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
                    "Исправь запрос и верни только корректный SQL."
                ),
            })

        try:
            sql = chat(messages, context=context)
        except Exception as e:
            last_error = {"error": f"LLM call failed: {e}", "sql": ""}
            continue

        sql = sql.strip().rstrip(";")

        # Шаг 1: безопасность (DDL/DML/multi-statement).
        safety_error = validate_sql(sql)
        if safety_error:
            last_error = {"error": safety_error, "sql": sql}
            continue

        # Шаг 2: EXPLAIN — проверка синтаксиса и существования объектов.
        explain_result = db.explain(sql)
        if not explain_result["valid"]:
            last_error = {"error": explain_result["error"], "sql": sql}
            err_text = explain_result.get("error", "")
            if "временно занята" in err_text:
                break
            continue

        # Шаг 3: выполнить.
        result = db.query_sql(sql)
        err_text = result.get("error", "") if isinstance(result, dict) else ""
        if result.get("status") == "error" and "временно занята" in err_text:
            last_error = {"error": err_text, "sql": sql}
            break

        return {
            "mode": "generated_sql",
            "status": result.get("status", "error"),
            "data": {"sql": sql, "result": result},
        }

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

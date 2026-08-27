"""
Режим: generated_sql — LLM генерирует SELECT по описанию на естественном языке.

Pipeline с ретраями:
  1. Получить схему БД (information_schema)
  2. LLM генерирует SQL по схеме + запросу пользователя
  3. Валидация безопасности (только SELECT, один statement)
  4. EXPLAIN (FORMAT JSON) — проверка синтаксиса без выполнения
  5. Выполнить SELECT
  6. Если EXPLAIN или валидация упали — retry до MAX_RETRIES раз
     с передачей предыдущей ошибки в LLM для исправления

Чтобы уменьшить класс ошибок «LLM выдумывает несуществующие таблицы»,
в system prompt подаётся:
  - жёсткий белый список доступных таблиц (whitelist);
  - retrieval-few-shot: 2-3 релевантных примера из реестра
    ``public.agent_predefined_scripts`` (по keyword-overlap с запросом).

Пример запуска через CLI:
    audit_analyze --mode generated_sql --query 'сколько аудитов было в 2024 по месяцам'
    audit_analyze --mode generated_sql --query 'топ-10 объектов по количеству нарушений'
    audit_analyze --mode generated_sql --query 'среднее количество нарушений на проверку'
"""


from __future__ import annotations

import re

from llm import chat
from skill_config import get_db_schema, get_db_tables, get_predefined_scripts_table

from column_hints import format_hints_block
from lib.utils.sql_safety import format_schema, validate_sql

MAX_RETRIES = 3


def _normalize(text: str) -> set[str]:
    """Токенизация для keyword-overlap: lower + split по небуквенным.

    Длина токена ≥ 3 (отсекает «и», «по», «в», «на», «of», «the»).
    """
    return {tok for tok in re.split(r"[^a-zа-яё0-9]+", (text or "").lower()) if len(tok) >= 3}


def _load_predefined_scripts(db) -> list[dict]:
    """Загрузить реестр предопределённых скриптов из DuckDB-кэша.

    Возвращает список ``{"name", "description", "sql_template", "tokens"}``
    с предвычисленным keyword-множеством для быстрого ranking'а.

    При ошибке (таблица отсутствует, нет прав) — возвращает пустой список.
    Это НЕ фатально для sql-режима: few-shot просто не подклеится.
    """
    table = get_predefined_scripts_table()
    if "." in table:
        schema, tbl = table.split(".", 1)
    else:
        schema, tbl = "main", table
    sql = (
        f'SELECT name, description, sql_template '
        f'FROM "{schema}"."{tbl}" ORDER BY name'
    )
    res = db.query_sql(sql)
    if res.get("status") != "success":
        return []
    out = []
    for row in res.get("rows", []):
        name = row.get("name") or ""
        desc = row.get("description") or ""
        sql_tpl = row.get("sql_template") or ""
        if not name:
            continue
        tokens = _normalize(f"{name} {desc}")
        out.append({"name": name, "description": desc, "sql_template": sql_tpl, "tokens": tokens})
    return out


def _select_few_shot(query: str, scripts: list[dict], limit: int = 2) -> str:
    """Выбрать top-N скриптов из реестра по keyword-overlap с запросом.

    Скоринг = |tokens(scr) ∩ tokens(query)|. Возвращает многострочный
    текстовый блок для system prompt или пустую строку, если реестр
    пуст / нет релевантных скриптов.
    """
    if not scripts:
        return ""
    q_tokens = _normalize(query)
    if not q_tokens:
        return ""
    scored = []
    for s in scripts:
        score = len(q_tokens & s["tokens"])
        if score > 0:
            scored.append((score, s))
    if not scored:
        return ""
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    chosen = [s for _, s in scored[:limit]]
    lines = ["Examples from the predefined registry (use as templates, adapt to the user's request):"]
    for s in chosen:
        lines.append(f"  -- «{s['description']}» →")
        lines.append(f"  {s['sql_template'].strip()}")
        lines.append("")
    return "\n".join(lines).rstrip()


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
            mode: "generated_sql"
            status: "success" | "error"
            data:
                sql: сгенерированный SQL
                result: результат выполнения (columns, rows, row_count)
            (при ошибке) message: описание ошибки
    """
    tables = get_db_tables() or None
    schema = db.get_schema(schema_name=get_db_schema(), table_names=tables)
    schema_text = format_schema(schema)
    qualified_tables = [f'"{get_db_schema()}"."{t}"' for t in (tables or [])]

    registry = _load_predefined_scripts(db)
    few_shot_block = _select_few_shot(query, registry, limit=2)
    few_shot_section = f"\n\n{few_shot_block}" if few_shot_block else ""

    system_prompt = (
        "You are a PostgreSQL expert. Return ONLY a safe SELECT query — no "
        "explanations, no markdown, no SQL wrapping.\n\n"
        "STRICT RULES:\n"
        "  1. Use ONLY these tables (whitelist, fully qualified):\n"
        f"     {', '.join(qualified_tables) if qualified_tables else '(none)'}\n"
        "  2. If the user's question cannot be answered from these tables, "
        "return the SQL that best approximates it (e.g. aggregate over the "
        "closest column). Never invent new tables.\n"
        "  3. Always schema-qualify table names."
        f"{format_hints_block()}"
        f"{few_shot_section}"
    )

    base_messages = [
        {"role": "system", "content": system_prompt},
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
                    f"Предыдущий SQL-запрос вызвал ошибку: {last_error['error']}.\n"
                    "Исправь запрос и верни только корректный SQL.\n"
                    "НАПОМИНАНИЕ: используй только таблицы из whitelist выше; "
                    "не придумывай новых таблиц; все имена таблиц — "
                    "полностью квалифицированные (schema.table)."
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
            "mode": "generated_sql",
            "status": result["status"],
            "data": {"sql": sql, "result": result},
        }

    # Все попытки исчерпаны
    detail = last_error or {"error": "неизвестная ошибка", "sql": ""}
    return {
        "mode": "generated_sql",
        "status": "error",
        "data": {
            "message": (
                f"Не удалось сгенерировать корректный SQL после "
                f"{MAX_RETRIES + 1} попыток. Последняя ошибка: {detail['error']}"
            ),
            "sql": detail.get("sql", ""),
        },
    }

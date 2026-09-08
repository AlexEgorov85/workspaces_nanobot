"""``sql_generator`` — skill-side helper для автономной генерации SQL.

Зачем: после перевода ``audit_analyzer`` на tool-only архитектуру
Agent получает два generic tool'а (``duckdb_query``, ``vector_search``)
и сам формирует SQL на основании ``references/schema.md`` и
``references/sql_guidance.md``. Если Agent предпочитает делегировать
формирование SQL — он может вызвать этот helper через ``exec``.

Helper **не выполняет SQL**. Он только генерирует и возвращает
SQL-строку. Выполнение — всегда через ``duckdb_query``.

## Контракт

CLI-режим::

    python scripts/sql_generator.py \
        --query "Сколько проверок за 2024 год?" \
        --schema-file references/schema.md

Возвращает JSON::

    {
      "status": "success",
      "sql": "SELECT COUNT(*) FROM oarb.audits WHERE actual_date >= ? AND actual_date < ?",
      "params": ["2024-01-01", "2025-01-01"]
    }

или::

    {
      "status": "error",
      "error_type": "...",
      "message": "..."
    }

## Использование

Helper может также импортироваться другим skill-side кодом::

    from workspace.skills.audit_analyzer.scripts.sql_generator import generate_sql

    result = generate_sql(
        query="...",
        schema_text=Path("references/schema.md").read_text(),
        tables_whitelist=["oarb.audits", "oarb.violations"],
    )

## Зависимости

* ``lib.services.llm_client.call_llm`` — generic LLM-клиент
  (OpenAI-compatible ``/chat/completions``);
* ``lib.services.llm_config.resolve_llm_config`` — резолв endpoint/модели
  из ``SETTINGS``;
* ``lib.utils.sql_safety.validate_sql`` — SELECT-only gate (последняя
  граница безопасности).

Skill не знает про конкретные таблицы — whitelist передаётся
параметром. По умолчанию helper не ограничивает whitelist (агент
передаёт его явно из ``references/schema.md``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


__all__ = ["generate_sql", "main", "build_system_prompt"]


_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*\n(.*?)```", re.DOTALL)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def build_system_prompt(
    *,
    schema_text: str,
    tables_whitelist: list[str] | None = None,
) -> str:
    """Сформировать system prompt для LLM-генерации SELECT.

    Args:
        schema_text: текст ``references/schema.md`` (или его подмножество).
        tables_whitelist: список fully-qualified имён таблиц, которые LLM
            разрешено использовать. ``None`` — без ограничений (не
            рекомендуется; helper принимает whitelist как параметр).

    Returns:
        Строка system prompt.
    """
    if tables_whitelist:
        qualified = ", ".join(f'"{t}"' for t in tables_whitelist)
        whitelist_rule = (
            f"STRICT RULE: use ONLY these tables (fully qualified): {qualified}."
        )
    else:
        whitelist_rule = (
            "STRICT RULE: use ONLY tables described in SCHEMA below. "
            "Never invent new tables."
        )

    return (
        "You generate safe PostgreSQL SELECT queries for the audit_analyzer skill.\n"
        "Return ONLY the SQL statement — no explanations, no markdown wrapping, "
        "no SQL comments.\n\n"
        f"{whitelist_rule}\n"
        "Additional rules:\n"
        "  - Use SELECT / WITH / EXPLAIN only. No DDL/DML.\n"
        "  - One statement per response.\n"
        "  - Use prepared parameters: ? (positional) or :name (named).\n"
        "  - For dates use YYYY-MM-DD format.\n"
        "  - Schema-qualify table names: schema.table.\n"
        "  - Prefer explicit JOIN over subqueries.\n\n"
        f"SCHEMA:\n{schema_text}\n"
    )


def _sanitize_sql_response(text: str) -> str:
    """Извлечь SQL из ответа LLM (markdown + think-блоки)."""
    cleaned = (text or "").strip()
    if "```" in cleaned:
        blocks = _SQL_FENCE_RE.findall(cleaned)
        if blocks:
            cleaned = blocks[-1].strip()
    cleaned = _THINK_RE.sub("", cleaned).strip()
    if not re.search(r"\b(SELECT|WITH|EXPLAIN)\b", cleaned, re.IGNORECASE):
        return ""
    return cleaned.rstrip(";").strip()


def generate_sql(
    *,
    query: str,
    schema_text: str,
    tables_whitelist: list[str] | None = None,
    cfg: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    """Сгенерировать SELECT по NL-запросу.

    Args:
        query: NL-запрос (например, «сколько проверок за 2024?»).
        schema_text: текст schema reference (см. ``references/schema.md``).
        tables_whitelist: опциональный whitelist fully-qualified таблиц.
        cfg: переопределение LLM-конфига (от ``resolve_llm_config``).
        max_tokens / temperature: переопределение параметров.

    Returns:
        ``{"status": "success", "sql": "..."}`` или
        ``{"status": "error", "error_type": "...", "message": "..."}``.
        Поле ``sql`` при ошибке — пустая строка.
    """
    if not query or not query.strip():
        return {
            "status": "error",
            "error_type": "invalid_query",
            "message": "query is empty",
            "sql": "",
        }

    system_prompt = build_system_prompt(
        schema_text=schema_text,
        tables_whitelist=tables_whitelist,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query},
    ]

    try:
        from lib.services.llm_client import call_llm

        raw = call_llm(
            messages,
            cfg=cfg,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as exc:
        return {
            "status": "error",
            "error_type": "llm_call_failed",
            "message": f"{type(exc).__name__}: {exc}",
            "sql": "",
        }

    sql = _sanitize_sql_response(raw)
    if not sql:
        return {
            "status": "error",
            "error_type": "empty_sql",
            "message": "LLM returned empty or non-SQL response",
            "sql": "",
        }

    from lib.utils.sql_safety import validate_sql

    safety_error = validate_sql(sql)
    if safety_error:
        return {
            "status": "error",
            "error_type": "sql_safety",
            "message": safety_error,
            "sql": sql,
        }

    return {"status": "success", "sql": sql}


def main(argv: list[str] | None = None) -> int:
    """CLI-обёртка: прочитать аргументы, вызвать ``generate_sql``, вернуть JSON."""
    parser = argparse.ArgumentParser(
        prog="sql_generator",
        description="Skill-side helper: сгенерировать SELECT по NL-запросу.",
    )
    parser.add_argument("--query", required=True, help="NL-запрос")
    parser.add_argument(
        "--schema-file",
        type=Path,
        default=None,
        help="Путь к файлу schema (например, references/schema.md). "
        "Если не указан — schema передаётся пустой.",
    )
    parser.add_argument(
        "--tables",
        nargs="*",
        default=None,
        help="Whitelist fully-qualified таблиц (например, oarb.audits oarb.violations).",
    )
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=None)

    args = parser.parse_args(argv)

    schema_text = ""
    if args.schema_file is not None:
        if not args.schema_file.is_file():
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error_type": "schema_file_not_found",
                        "message": f"{args.schema_file} not found",
                        "sql": "",
                    },
                    ensure_ascii=False,
                )
            )
            return 2
        schema_text = args.schema_file.read_text(encoding="utf-8")

    result = generate_sql(
        query=args.query,
        schema_text=schema_text,
        tables_whitelist=args.tables,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
    )

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    sys.exit(main())

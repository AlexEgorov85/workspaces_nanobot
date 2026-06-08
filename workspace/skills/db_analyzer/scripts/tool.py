from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import config
import predefined_mode
import sql_mode
import vector_mode
from database import Database


@tool_parameters({
    "type": "object",
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["predefined", "sql", "vector"],
            "description": "Режим работы: predefined — готовые SQL-скрипты, sql — генерация SQL через LLM, vector — семантический поиск",
        },
        "script": {
            "type": "string",
            "description": "Имя скрипта для mode=predefined. Доступны: analytics_by_year_month, violations_by_type, top_audited_objects, audit_effectiveness, audit_dynamics, audit_types_stats",
        },
        "params": {
            "type": "object",
            "description": "Параметры скрипта для mode=predefined (ключ-значение). Например: {\"year\": 2024, \"limit\": 10}",
            "additionalProperties": {"type": "string"},
        },
        "query": {
            "type": "string",
            "description": "Запрос на естественном языке для mode=sql или mode=vector",
        },
        "index_name": {
            "type": "string",
            "description": "Имя FAISS-индекса для mode=vector (без .faiss). По умолчанию: audits_index",
        },
        "top_k": {
            "type": "integer",
            "description": "Количество результатов для mode=vector (по умолчанию 5). Игнорируется при threshold",
            "default": 5,
        },
        "threshold": {
            "type": "number",
            "description": "Порог схожести 0.0–1.0 для mode=vector. Если задан — все результаты выше порога, top_k игнорируется",
        },
    },
    "required": ["mode"],
})
class DbAnalyzerTool(Tool):
    name = "db_analyzer"
    description = (
        "Анализ PostgreSQL-базы аудиторских проверок. "
        "Три режима: predefined (готовые SQL-скрипты с параметрами), "
        "sql (LLM генерирует SELECT по описанию), "
        "vector (семантический поиск по FAISS-индексу)."
    )

    def __init__(self):
        self._name = "db_analyzer"

    async def execute(self, **kwargs: Any) -> str:
        try:
            mode = kwargs.get("mode")
            if mode not in ("predefined", "sql", "vector"):
                return f"Ошибка: неверный режим '{mode}'. Допустимые: predefined, sql, vector"

            if mode == "predefined":
                return await self._run_predefined(kwargs)
            elif mode == "sql":
                return await self._run_sql(kwargs)
            else:
                return await self._run_vector(kwargs)
        except Exception as e:
            return f"Ошибка db_analyzer: {e}\n{traceback.format_exc()}"

    async def _run_predefined(self, kwargs: dict) -> str:
        script_name = kwargs.get("script")
        if not script_name:
            return "Ошибка: для mode=predefined укажите script"

        params = kwargs.get("params") or {}
        db_config = _load_db_config()
        async with Database(db_config) as db:
            result = await predefined_mode.run(
                script_name, db, params=params,
                index_dir=_index_dir(),
            )

        return _format_result(result, "predefined", script_name)

    async def _run_sql(self, kwargs: dict) -> str:
        query = kwargs.get("query")
        if not query:
            return "Ошибка: для mode=sql укажите query"

        context = kwargs.get("context")
        db_config = _load_db_config()
        async with Database(db_config) as db:
            result = await sql_mode.run(query, db, context=context)

        return _format_result(result, "sql")

    async def _run_vector(self, kwargs: dict) -> str:
        query = kwargs.get("query")
        if not query:
            return "Ошибка: для mode=vector укажите query"

        index_name = kwargs.get("index_name") or "audits_index"
        top_k = kwargs.get("top_k") or 5
        threshold = kwargs.get("threshold")
        index_path = _index_dir()

        result = await vector_mode.run(
            query, index_name, index_path=index_path,
            top_k=top_k, threshold=threshold,
        )

        return _format_result(result, "vector")


def _load_db_config() -> dict:
    return config.load_db_config()


def _index_dir() -> str:
    return config.get_vector_index_path()


def _format_result(result: dict, mode: str, script_name: str | None = None) -> str:
    parts: list[str] = []

    if result.get("status") == "error":
        msg = result.get("data", {}).get("message", "Неизвестная ошибка")
        return f"Ошибка: {msg}"

    data = result.get("data", {})

    if mode == "predefined":
        sql = data.get("sql", "")
        if sql:
            parts.append(f"SQL: {sql}")
        res = data.get("result", {})
        rows = res.get("rows", [])
        cols = res.get("columns", [])
        parts.append(f"Строк: {len(rows)}")
        if cols:
            parts.append(f"Колонки: {', '.join(cols)}")
        if rows:
            parts.append(f"Данные (первые 20): {json.dumps(rows[:20], ensure_ascii=False, indent=2, default=str)}")

    elif mode == "sql":
        sql = data.get("sql", "")
        if sql:
            parts.append(f"Сгенерированный SQL: {sql}")
        res = data.get("result", {})
        rows = res.get("rows", [])
        cols = res.get("columns", [])
        parts.append(f"Строк: {len(rows)}")
        if cols:
            parts.append(f"Колонки: {', '.join(cols)}")
        if rows:
            parts.append(f"Данные (первые 20): {json.dumps(rows[:20], ensure_ascii=False, indent=2, default=str)}")

    elif mode == "vector":
        results = data.get("results", [])
        parts.append(f"Найдено результатов: {len(results)}")
        for i, r in enumerate(results, 1):
            score = r.get("score", 0)
            content = r.get("content", "")
            parts.append(f"  {i}. score={score:.4f} — {content[:200]}")

    return "\n".join(parts)

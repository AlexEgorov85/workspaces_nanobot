"""AuditAnalyzerTool — Tool регистрируется при старте бота в gateway.py."""

import json
import traceback

from nanobot.agent.tools.base import Tool, tool_parameters

from .config import load_db_config
from .scripts_registry import SCRIPTS_REGISTRY, ScriptDefinition
from .predefined import build_sql, list_available
from .database import execute_query
from . import sql_mode, vector_mode


@tool_parameters({
    "type": "object",
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["predefined", "vector", "sql"],
            "description": (
                "Режим анализа:\n"
                "  predefined — заготовленные SQL-скрипты (быстрые стандартные отчёты)\n"
                "  vector — семантический поиск по FAISS-индексу (поиск по смыслу)\n"
                "  sql — генерация кастомного SQL через LLM + авто-валидация"
            ),
        },
        "script": {
            "type": "string",
            "description": (
                "Имя скрипта для mode=predefined. Доступны:\n"
                "  analytics_by_year_month — аналитика по годам/месяцам\n"
                "  violations_by_type — статистика нарушений по типам\n"
                "  top_audited_objects — топ объектов по проверкам\n"
                "  audit_effectiveness — оценка эффективности проверок\n"
                "  audit_dynamics — динамика по периодам\n"
                "  audit_types_stats — статистика по типам проверок"
            ),
        },
        "index_name": {
            "type": "string",
            "description": "Имя векторного индекса для mode=vector (список индексов см. в навыке audit-analyzer).",
        },
        "query": {
            "type": "string",
            "description": "Текстовый запрос для mode=vector (поиск по смыслу) или sql (NL → SQL). Для predefined не используется.",
        },
        "params": {
            "type": "object",
            "description": (
                "Параметры скрипта для mode=predefined (key: value). "
                "Пример: {\"year\": 2024, \"limit\": 10, \"date_from\": \"2024-01-01\"}"
            ),
        },
        "top_k": {
            "type": "integer",
            "description": "Количество результатов для mode=vector (по умолчанию 5)",
        },
        "threshold": {
            "type": "number",
            "description": "Порог схожести для mode=vector (0.0–1.0). Если задан, top_k игнорируется",
        },
    },
    "required": ["mode"],
})
class AuditAnalyzerTool(Tool):
    """Анализ аудиторских проверок: SQL-отчёты, векторный поиск, генерация SQL."""

    name = "audit_analyze"
    description = (
        "Инструмент для анализа аудиторских проверок. Три режима:\n"
        "1) predefined — готовые SQL-скрипты. Выбор по имени (script): "
        "analytics_by_year_month, violations_by_type, top_audited_objects, "
        "audit_effectiveness, audit_dynamics, audit_types_stats. "
        "Параметры передаются через params.\n"
        "2) vector — семантический поиск по FAISS-индексу документов (нарушения, рекомендации)\n"
        "3) sql — генерация кастомного SQL-запроса через LLM по описанию на русском языке, "
        "автоматическая валидация безопасности и выполнение"
    )

    def __init__(self):
        self._name = "audit_analyze"

    async def execute(
        self,
        mode: str,
        script: str | None = None,
        query: str | None = None,
        params: dict | None = None,
        index_name: str | None = None,
        top_k: int | None = None,
        threshold: float | None = None,
    ) -> str:
        try:
            if mode == "predefined":
                return await self._predefined(script, query, params)
            elif mode == "vector":
                return await self._vector(query, index_name, top_k, threshold)
            elif mode == "sql":
                return await self._sql(query)
            return f"Error: неизвестный режим '{mode}'. Допустимы: predefined, vector, sql"
        except Exception as e:
            return f"Error: {e}\n\n{traceback.format_exc()}"

    async def _predefined(
        self, script: str | None, query: str | None, params: dict | None,
    ) -> str:
        if not script:
            return f"Error: для mode=predefined требуется параметр script. Доступны: {list_available()}"

        s = SCRIPTS_REGISTRY.get(script)
        if not s:
            return f"Error: скрипт '{script}' не найден. Доступны: {list_available()}"

        merged = _user_params_to_fragments(s, params)
        sql, sql_params = build_sql(s, merged)
        result = await execute_query(load_db_config(), sql, sql_params)
        return _format_predefined(result, s.name, sql, merged)

    async def _vector(
        self, query: str | None, index_name: str | None, top_k: int | None, threshold: float | None,
    ) -> str:
        if not query:
            return "Error: для mode=vector требуется параметр query"
        if not index_name:
            return "Error: для mode=vector требуется параметр index_name"
        result = await vector_mode.run(query, index_name, top_k=top_k or 5, threshold=threshold)
        return _format_result(result, "vector")

    async def _sql(self, query: str | None) -> str:
        if not query:
            return "Error: для mode=sql требуется параметр query"
        db_cfg = load_db_config()
        result = await sql_mode.run(query, db_cfg)
        return _format_result(result, "sql")


def _user_params_to_fragments(script: ScriptDefinition, params: dict) -> dict:
    """Преобразовать user-параметры (year, date_from, limit, ...) в типизированный dict

    для DynamicQueryBuilder. Ключи должны совпадать с именами в ScriptDefinition.parameters.
    """
    frag: dict = {}

    for k, v in (params or {}).items():
        if v is None or v == "":
            continue
        if k in script.parameters:
            frag[k] = v
        # Алиас: пользовательский ключ → имя параметра скрипта
        elif k == "audited_object" and "auditee_entity" in script.parameters:
            frag["auditee_entity"] = v
        elif k == "audit_type" and "audit_type" in script.parameters:
            frag["audit_type"] = v
        elif k == "violation_code" and "violation_code" in script.parameters:
            frag["violation_code"] = v

    return frag


def _format_result(result: dict, mode: str) -> str:
    status = result.get("status", "error")
    data = result.get("data", {})
    lines = [f"## Результат анализа (mode: {mode})", f"**Статус**: {status}", ""]
    if status != "success":
        msg = data.get("message") or data.get("db_error") or "неизвестная ошибка"
        lines.append(f"**Ошибка**: {msg}")
        return "\n".join(lines)

    if mode == "predefined":
        script_name = data.get("script_name", "")
        result_data = data.get("result", {})
        rows = result_data.get("rows", [])
        sql = data.get("sql", "")
        params = data.get("parameters", {})
        lines.append(f"**Скрипт**: {script_name}")
        if params:
            lines.append(f"**Параметры**: {json.dumps(params, ensure_ascii=False)}")
        lines.append(f"**Записей**: {len(rows)}")
        lines.append(f"**SQL**: `{sql}`")
        if rows:
            lines.append(""); lines.append("### Данные"); lines.append(_format_rows(rows))

    elif mode == "vector":
        results = data.get("results", [])
        lines.append(f"**Найдено**: {len(results)} документов")
        for r in results:
            score = r.get("score", 0)
            content = r.get("content", "")
            source = r.get("source", "")
            lines.append("")
            lines.append(f"--- (score: {score:.4f}, source: {source})")
            lines.append(_truncate(content, 500))

    elif mode == "sql":
        result_data = data.get("result", {})
        rows = result_data.get("rows", [])
        sql = data.get("sql", "")
        lines.append(f"**Записей**: {len(rows)}")
        lines.append(f"**Сгенерированный SQL**: `{sql}`")
        if rows:
            lines.append(""); lines.append("### Данные"); lines.append(_format_rows(rows))
    return "\n".join(lines)


def _format_predefined(result: dict, script_name: str, sql: str, params: dict) -> str:
    lines = [
        "## Результат анализа (mode: predefined)",
        f"**Статус**: {result.get('status', 'error')}", "",
        f"**Скрипт**: {script_name}",
    ]
    if params:
        lines.append(f"**Параметры**: {json.dumps(params, ensure_ascii=False)}")
    if result.get("status") != "success":
        lines.append(f"**Ошибка**: {result.get('error', 'неизвестная ошибка')}")
        lines.append(f"**SQL**: `{sql}`")
        return "\n".join(lines)
    rows = result.get("rows", [])
    lines.append(f"**Записей**: {len(rows)}")
    lines.append(f"**SQL**: `{sql}`")
    if rows:
        lines.append(""); lines.append("### Данные"); lines.append(_format_rows(rows))
    return "\n".join(lines)


def _format_rows(rows: list[dict]) -> str:
    if not rows:
        return "(нет данных)"
    cols = list(rows[0].keys())
    header = " | ".join(cols)
    sep = " | ".join(["---"] * len(cols))
    data_lines = []
    for row in rows:
        data_lines.append(" | ".join(str(row.get(c, "")) for c in cols))
    return header + "\n" + sep + "\n" + "\n".join(data_lines)


def _truncate(text: str, max_len: int) -> str:
    return text[:max_len] + "..." if len(text) > max_len else text

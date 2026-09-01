"""Общая логика NL→SELECT pipeline для tool'а ``nl_sql_generate``.

Используется:
  * ``workspace/tools/nl_sql_generate.py`` — обёртка для агента.

После перевода skill'а `audit_analyzer` на tool-only skill больше
не имеет собственного CLI-обёртки; всё NL→SELECT-взаимодействие
идёт через ``NlSqlGenerateTool`` → ``NlSqlRunner``.

Pipeline:
  1. Whitelist таблиц из ``TableRegistry.table_names()``.
  2. Схема — ``SchemaFormatter.format_for_llm`` (internal service).
  3. Few-shot — ``TableRegistry.resources_by_label("scripts_registry")``
     (несколько примеров из реестра по keyword-overlap).
  4. LLM-цикл с retry (default 3): generate → sanitize → validate_sql →
     provider.explain → provider.query_sql.
  5. Возврат результата в формате generated_sql_mode (для back-compat).

Зависимости (только shared infra — TARGET §22.3, §22.8):
  * ``lib.services.cache_provider_impl.CacheProvider`` — query / explain;
  * ``lib.services.table_registry.table_registry`` — whitelist + few-shot;
  * ``lib.services.schema_formatter.SchemaFormatter`` — описание схемы;
  * ``lib.services.llm_client.call_llm`` — LLM-вызов;
  * ``lib.utils.sql_safety.validate_sql`` — security boundary;
  * ``lib.utils.text_utils.sanitize_value`` — JSON-сериализация.

НЕ импортирует ``workspace.skills.*`` — это shared infra.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from lib.services.llm_client import call_llm
from lib.services.schema_formatter import SchemaFormatter
from lib.services.table_registry import table_registry
from lib.utils.sql_safety import validate_sql


__all__ = ["NlSqlRunner", "NlSqlRunnerConfig", "NlSqlResult"]


class _CacheProviderProtocol(Protocol):
    """Минимальный интерфейс CacheProvider для NlSqlRunner."""

    def query_sql(self, sql: str, params: list | None = None) -> dict[str, Any]: ...
    def explain(self, sql: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class NlSqlRunnerConfig:
    """Параметры pipeline NL→SELECT.

    Все поля опциональны (есть разумные дефолты); передаются через
    ``NlSqlRunner(..., config=...)`` или из конфига tool'а/skill'а.
    """

    max_retries: int = 3
    schema_max_chars: int = 12_000
    few_shot_top_n: int = 2
    timeout_lock_msg: str = "временно занята"
    """Сообщение EXPLAIN/query, при котором НЕ делаем retry (lock-ситуация)."""


@dataclass
class NlSqlResult:
    """Результат одной попытки pipeline (внутренний helper)."""

    sql: str = ""
    status: str = "error"
    error: str | None = None
    error_type: str = "generation_failed"
    rows: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    row_count: int = 0


class NlSqlRunner:
    """Общая логика NL→SELECT pipeline.

    Args:
        provider: ``CacheProvider`` (см. ``lib.services.cache_provider_impl``).
        llm_config: конфиг LLM (от ``resolve_llm_config``); если ``None`` —
            резолвится внутри через ``call_llm`` по умолчанию.
        schema_formatter: internal ``SchemaFormatter``; если ``None`` —
            используется новый экземпляр (дефолт).
        config: ``NlSqlRunnerConfig`` (max_retries, schema_max_chars, ...).
    """

    _TOKEN_SPLIT_RE = re.compile(r"[^a-zа-яё0-9]+")

    def __init__(
        self,
        *,
        provider: _CacheProviderProtocol,
        llm_config: dict[str, Any] | None = None,
        schema_formatter: SchemaFormatter | None = None,
        config: NlSqlRunnerConfig | None = None,
    ) -> None:
        self._provider = provider
        self._llm_config = llm_config
        self._schema_formatter = schema_formatter or SchemaFormatter()
        self._config = config or NlSqlRunnerConfig()

    def run(
        self,
        query: str,
        *,
        context: list[dict] | None = None,
        no_few_shot: bool = False,
        hints_block: str = "",
        history: list[NlSqlResult] | None = None,
    ) -> dict[str, Any]:
        """Выполнить NL→SELECT pipeline.

        Args:
            query: запрос на естественном языке.
            context: история чата (опц.) — пробрасывается в LLM.
            no_few_shot: пропустить retrieval few-shot из реестра.
            hints_block: дополнительный текстовый блок для system prompt
                (например, подсказки из ``column_descriptions``).
            history: стек предыдущих ошибок/результатов (для retry-цикла);
                обычно не передаётся — runner сам ведёт историю.

        Returns:
            dict в формате ``generated_sql_mode.run()``:
              ``{"mode": "generated_sql", "status", "data": {"sql", "result"}}``.
        """
        tables = self._resolve_tables()
        if not tables:
            return self._error_no_registry()
        schema = self._resolve_schema()

        registry = [] if no_few_shot else self._load_predefined_scripts()
        few_shot_block = (
            "" if no_few_shot else self._select_few_shot(query, registry)
        )

        system_prompt = self._build_system_prompt(
            tables=tables,
            schema=schema,
            hints_block=hints_block,
            few_shot_block=few_shot_block,
        )

        base_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Schema:\n{schema}\n\nRequest: {query}"},
        ]

        last_error: dict[str, Any] | None = None

        for attempt in range(self._config.max_retries + 1):
            messages = list(base_messages)
            if attempt > 0 and last_error:
                messages.append({"role": "assistant", "content": last_error["sql"]})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Предыдущий SQL-запрос вызвал ошибку: "
                        f"{last_error['error']}.\nИсправь запрос и верни только "
                        "корректный SQL.\nНАПОМИНАНИЕ: используй только таблицы "
                        "из whitelist выше; не придумывай новых таблиц; все имена "
                        "таблиц — полностью квалифицированные (schema.table)."
                    ),
                })

            try:
                raw = self._call_llm(messages, context=context)
            except Exception as exc:
                last_error = {"error": f"LLM call failed: {exc}", "sql": ""}
                continue

            sql = self._sanitize_sql_response(raw)
            if not sql:
                last_error = {"error": "LLM вернул пустой SQL", "sql": ""}
                continue

            safety_error = validate_sql(sql)
            if safety_error:
                last_error = {"error": safety_error, "sql": sql}
                continue

            explain = self._provider.explain(sql)
            if not explain.get("valid"):
                err = explain.get("error", "EXPLAIN failed")
                last_error = {"error": err, "sql": sql}
                if self._config.timeout_lock_msg in err:
                    break
                continue

            result = self._provider.query_sql(sql)
            if result.get("status") == "error":
                err = result.get("error", "query failed")
                last_error = {"error": err, "sql": sql}
                if self._config.timeout_lock_msg in err:
                    break
                continue

            return {
                "mode": "generated_sql",
                "status": "success",
                "data": {"sql": sql, "result": result},
            }

        detail = last_error or {"error": "неизвестная ошибка", "sql": ""}
        attempts = self._config.max_retries + 1
        return {
            "mode": "generated_sql",
            "status": "error",
            "data": {
                "message": (
                    f"Не удалось сгенерировать корректный SQL после {attempts} "
                    f"попыток. Последняя ошибка: {detail['error']}"
                ),
                "sql": detail.get("sql", ""),
            },
        }

# ---------- helpers --------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {
            tok
            for tok in NlSqlRunner._TOKEN_SPLIT_RE.split((text or "").lower())
            if len(tok) >= 3
        }

    def _resolve_tables(self) -> list[str]:
        return list(table_registry.table_names())

    def _resolve_schema(self) -> str:
        names = self._schema_formatter.list_schema_names()
        return names[0] if names else "main"

    def _load_predefined_scripts(self) -> list[dict[str, Any]]:
        """Загрузить реестр скриптов из DuckDB-таблицы ``scripts_registry``.

        Поиск через ``TableRegistry.resources_by_label("scripts_registry")`` —
        не зависит от конкретного имени таблицы в PG.
        """
        resources = table_registry.resources_by_label("scripts_registry")
        if not resources:
            return []
        table = resources[0].name
        if "." in table:
            schema, tbl = table.split(".", 1)
        else:
            schema, tbl = "main", table
        sql = (
            f'SELECT name, description, sql_template '
            f'FROM "{schema}"."{tbl}" ORDER BY name'
        )
        result = self._provider.query_sql(sql)
        if result.get("status") != "success":
            return []
        out: list[dict[str, Any]] = []
        for row in result.get("rows", []) or []:
            name = row.get("name") or ""
            desc = row.get("description") or ""
            tpl = row.get("sql_template") or ""
            if not name:
                continue
            out.append({
                "name": name,
                "description": desc,
                "sql_template": tpl,
                "tokens": self._tokenize(f"{name} {desc}"),
            })
        return out

    def _select_few_shot(self, query: str, scripts: list[dict]) -> str:
        if not scripts:
            return ""
        q_tokens = self._tokenize(query)
        if not q_tokens:
            return ""
        scored: list[tuple[int, dict]] = []
        for s in scripts:
            score = len(q_tokens & s["tokens"])
            if score > 0:
                scored.append((score, s))
        if not scored:
            return ""
        scored.sort(key=lambda x: (-x[0], x[1]["name"]))
        chosen = [s for _, s in scored[: self._config.few_shot_top_n]]
        lines = [
            "Examples from the predefined registry "
            "(use as templates, adapt to the user's request):"
        ]
        for s in chosen:
            lines.append(f"  -- «{s['description']}» →")
            lines.append(f"  {s['sql_template'].strip()}")
            lines.append("")
        return "\n".join(lines).rstrip()

    def _build_system_prompt(
        self,
        *,
        tables: list[str],
        schema: str,
        hints_block: str,
        few_shot_block: str,
    ) -> str:
        qualified = [f'"{schema}"."{t.split(".", 1)[-1]}"' for t in tables]
        qualified_joined = ", ".join(qualified) if qualified else "(none)"
        hints_section = f"\n{hints_block}" if hints_block else ""
        few_shot_section = f"\n\n{few_shot_block}" if few_shot_block else ""
        return (
            "You are a PostgreSQL expert. Return ONLY a safe SELECT query — no "
            "explanations, no markdown, no SQL wrapping.\n\n"
            "STRICT RULES:\n"
            "  1. Use ONLY these tables (whitelist, fully qualified):\n"
            f"     {qualified_joined}\n"
            "  2. If the user's question cannot be answered from these tables, "
            "return the SQL that best approximates it (e.g. aggregate over the "
            "closest column). Never invent new tables.\n"
            "  3. Always schema-qualify table names."
            f"{hints_section}"
            f"{few_shot_section}"
        )

    def _call_llm(
        self, messages: list[dict], *, context: list[dict] | None = None
    ) -> str:
        return call_llm(
            messages,
            cfg=self._llm_config,
            context=context,
        )

    @staticmethod
    def _sanitize_sql_response(text: str) -> str:
        """Извлечь SQL из ответа LLM (markdown + think-блоки).

        Если в результате очистки не осталось SQL (нет ``SELECT``/``WITH``/
        ``EXPLAIN``) — возвращает пустую строку. Это сигнал для retry-цикла.
        """
        cleaned = (text or "").strip()
        if "```" in cleaned:
            blocks = re.findall(r"```(?:sql)?\s*\n(.*?)```", cleaned, re.DOTALL)
            if blocks:
                cleaned = blocks[-1].strip()
        cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()
        cleaned = re.sub(
            r"```xml-think\s*\n.*?```", "", cleaned, flags=re.DOTALL
        ).strip()
        cleaned = re.sub(
            r"^[^\S\n]*think:[^\n]*\n", "", cleaned, flags=re.MULTILINE
        ).strip()
        if not re.search(r"\b(SELECT|WITH|EXPLAIN)\b", cleaned, re.IGNORECASE):
            return ""
        return cleaned.rstrip(";").strip()

    @staticmethod
    def _error_no_registry() -> dict[str, Any]:
        return {
            "mode": "generated_sql",
            "status": "error",
            "data": {
                "message": (
                    "TableRegistry пуст: ни одна таблица не зарегистрирована. "
                    "Настройте project.json::skills.<name>.tables[] и "
                    "ApplicationContext._auto_register_skills."
                ),
                "sql": "",
            },
        }

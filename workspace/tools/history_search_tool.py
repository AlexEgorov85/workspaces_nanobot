"""``history_search`` — generic-инструмент агента для поиска по истории.

Реализует запрос пользователя «что было в старых сообщениях / что агент
уже делал»: ищет события в долговечном журнале ``agent_gateway_logs``
(пишется ``DbLoggingService`` и ``ContextCompactionService``) и возвращает
агенту выжимку, которую можно добавить в контекст.

Журнал переживает context compaction (в отличие от ``agent_session_messages``
и ``agent_conversation_messages``), поэтому инструмент — основной способ
агента «вспомнить» выпавшие из контекста детали (результаты tool-вызовов,
свои прошлые ответы, факт сжатия).

Конфиг читается из секции ``tools.history_search`` в ``config.json``::

    {
      "tools": {
        "history_search": {
          "enable": true,
          "max_rows": 50,
          "max_result_chars": 8000
        }
      }
    }

Безопасность: все фильтры передаются позиционными ``%s``-параметрами,
без интерполяции строк в SQL (как в ``duckdb_query``). Observability —
через штатный ``tool_audit_hook``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar

from nanobot.agent.tools.base import Tool, tool_parameters
from pydantic import BaseModel, Field

from lib.utils.text_utils import truncate_middle


class HistorySearchToolConfig(BaseModel):
    """Конфиг секции ``tools.history_search`` в ``config.json``."""

    enable: bool = True
    max_rows: int = Field(default=50, ge=1, le=500)
    max_result_chars: int = Field(default=8000, ge=200, le=100000)


@tool_parameters({
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Подстрока для поиска по истории (ILIKE, регистронезависимо). "
                "Ищется в summary события и в JSON-теле payload (включая "
                "пути файлов, doc_id, текст диалога). Можно оставить "
                "пустым, если нужна фильтрация только по event_type / "
                "времени. Примеры: query='договор' (найти упоминания "
                "договора), query='.pdf' (найти файлы по расширению), "
                "query='риски' (найти обсуждение рисков в llm_call)."
            ),
        },
        "event_type": {
            "type": "string",
            "enum": [
                "context_compacted",
                "tool_call",
                "tool_result",
                "llm_call",
                "run_finished",
                "subagent_run_finished",
                "inbound",
            ],
            "description": (
                "Тип события (опционально). Доступные типы, реально "
                "пишущиеся в журнал:\n"
                "  • context_compacted — факт сжатия контекста (что именно "
                "заархивировано, сколько токенов до/после).\n"
                "  • tool_call — вызов инструмента агентом, включая "
                "аргументы (пути файлов, переданные пользователем или "
                "агентом, лежат здесь).\n"
                "  • tool_result — результат инструмента (может содержать "
                "пути созданных файлов, doc_id и пр.).\n"
                "  • llm_call — полный промпт итерации LLM, включая вопросы "
                "пользователя (поиск по тексту диалога).\n"
                "  • run_finished — прошлый финальный ответ агента "
                "пользователю.\n"
                "  • subagent_run_finished — ответ под-агента.\n"
                "  • inbound — входящее сообщение пользователя.\n"
                "Если не указан — ищутся все типы. Для поиска файлов "
                "используй tool_call/tool_result (аргументы и результаты "
                "tool-вызовов) и llm_call (текст диалога), а НЕ выдуманные "
                "типы file_* / document_summarized."
            ),
        },
        "tool_name": {
            "type": "string",
            "description": (
                "Имя инструмента для фильтрации (опционально). "
                "Применимо только при event_type='tool_call' или "
                "event_type='tool_result'. Удобно для поиска "
                "истории конкретного инструмента: tool_name='compact_context' "
                "найдёт все его вызовы и результаты. Соответствует "
                "колонке ``name`` в ``agent_gateway_logs``. "
                "Если не указан — фильтрация по имени инструмента "
                "не применяется (поиск по всем инструментам в рамках "
                "event_type)."
            ),
        },
        "since": {
            "type": "string",
            "description": "Нижняя граница времени (ISO-8601), опционально.",
        },
        "until": {
            "type": "string",
            "description": "Верхняя граница времени (ISO-8601), опционально.",
        },
        "session_scope": {
            "type": "string",
            "enum": ["current", "all"],
            "description": (
                "Область поиска: 'current' — только текущая сессия (по "
                "умолчанию; используй, когда пользователь ссылается на "
                "'тот файл из нашего разговора'), 'all' — по всем сессиям "
                "(кросс-чатовый поиск, когда неизвестно, в какой сессии "
                "было событие)."
            ),
            "default": "current",
        },
        "limit": {
            "type": "integer",
            "description": "Максимум событий в ответе (по умолчанию из конфига).",
            "minimum": 1,
        },
    },
    "required": [],
})
class HistorySearchTool(Tool):
    """Искать события в долговечном журнале агента (переживает compaction)."""

    config_key: ClassVar[str] = "history_search"

    def __init__(self, *, config: HistorySearchToolConfig) -> None:
        self.config = config

    @classmethod
    def config_cls(cls):
        return HistorySearchToolConfig

    @classmethod
    def _read_settings_section(cls, ctx: Any) -> dict[str, Any]:
        """Прочитать ``tools.history_search`` из ``ctx._settings_ref``."""
        settings = getattr(ctx, "_settings_ref", None)
        if settings is None:
            return {}
        try:
            tools_section = settings.tools
        except AttributeError:
            return {}
        if tools_section is None:
            return {}
        try:
            section = getattr(tools_section, cls.config_key)
        except AttributeError:
            return {}
        if section is None:
            return {}
        if isinstance(section, dict):
            return dict(section)
        try:
            return dict(section)
        except Exception:
            return {"enable": bool(getattr(section, "enable", True))}

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        section = cls._read_settings_section(ctx)
        return bool(section.get("enable", True))

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        section = cls._read_settings_section(ctx)
        try:
            config = cls.config_cls()(**section)
        except Exception:
            config = cls.config_cls()()
        return cls(config=config)

    @property
    def name(self) -> str:
        return "history_search"

    @property
    def description(self) -> str:
        return (
            "Search the agent's durable event history (agent_gateway_logs) "
            "for past activity that survived context compaction. Use it to "
            "recover details from older messages the agent can no longer see "
            "in its live context (after a 'context_compacted' event), or to "
            "find previous results of its own work. CALL THIS BEFORE ANSWERING "
            "whenever the user references something not present in the current "
            "context: 'that file we discussed', 'my report from last week', "
            "'what did you answer about X', 'reprocess that contract', or "
            "after a 'context compressed' notice. Available event types: "
            "context_compacted (fact of compaction), tool_call (tool "
            "invocations + their args, incl. FILE PATHS passed by user/agent), "
            "tool_result (tool outputs, incl. created file paths / doc_id), "
            "llm_call (full prompt with user questions — search dialogue text "
            "here), run_finished (previous final answers), "
            "subagent_run_finished, inbound (user messages). For files, search "
            "tool_call / tool_result / llm_call — NEVER invented types like "
            "file_attached / file_created / document_summarized (not logged). "
            "Supports text query (ILIKE), event_type filter, tool_name filter "
            "(only meaningful for tool_call/tool_result; e.g. tool_name='compact_context' "
            "finds all calls/results of compact_context), time range "
            "(since/until ISO-8601), session_scope ('current' default | 'all'). "
            "Returns JSON {status, count, session_scope, truncated, "
            "events:[{timestamp, event_type, name, level, summary, payload}]}; "
            "payload is a JSON-string. After getting results: parse payload "
            "(fields path/doc_id/args/result usually survive truncation), "
            "reuse any found path/doc_id instead of redoing work; if empty, "
            "say 'not found in history' — do not fabricate."
        )

    async def execute(
        self,
        *,
        query: str | None = None,
        event_type: str | None = None,
        tool_name: str | None = None,
        since: str | None = None,
        until: str | None = None,
        session_scope: str = "current",
        limit: int | None = None,
        **_kwargs: Any,
    ) -> str:
        allow_all = session_scope == "all"
        session_id = None
        if not allow_all:
            session_id = _current_session_key()

        effective_limit = min(int(limit or self.config.max_rows), self.config.max_rows)

        clauses: list[str] = []
        params: list[Any] = []

        # scope: (allow_all OR session_id = %s)
        clauses.append("(%s OR session_id = %s)")
        params.append(allow_all)
        params.append(session_id or "")

        # event_type: IS NULL => пропускаем фильтр
        clauses.append("(%s IS NULL OR event_type = %s)")
        params.append(event_type)
        params.append(event_type)

        # tool_name: фильтр по имени инструмента. Имеет смысл только
        # в комбинации с event_type ∈ {tool_call, tool_result} — но
        # на уровне БД мы не валидируем комбинацию (если указан иной
        # event_type, фильтр просто не даст совпадений; это безопасно).
        if tool_name:
            clauses.append("name = %s")
            params.append(tool_name)

        if query:
            like = f"%{query}%"
            clauses.append("(summary ILIKE %s OR payload::text ILIKE %s)")
            params.append(like)
            params.append(like)

        if since:
            clauses.append('"timestamp" >= %s')
            params.append(since)

        if until:
            clauses.append('"timestamp" <= %s')
            params.append(until)

        schema, table = _log_table()
        sql = (
            f'SELECT "timestamp", event_type, name, level, summary, payload '
            f'FROM "{schema}"."{table}" '
            f"WHERE {' AND '.join(clauses)} "
            'ORDER BY "timestamp" DESC LIMIT %s'
        )
        params.append(effective_limit)

        try:
            from utils.db import fetch
        except Exception as exc:
            return self._error("import_error", str(exc))

        try:
            rows = await asyncio.to_thread(fetch, sql, *params)
        except Exception as exc:
            return self._error("db_error", str(exc))

        events = []
        for row in rows or []:
            payload = row.get("payload")
            if isinstance(payload, (dict, list)):
                try:
                    payload_text = json.dumps(payload, ensure_ascii=False, default=str)
                except Exception:
                    payload_text = str(payload)
            else:
                payload_text = str(payload) if payload is not None else ""
            events.append({
                "timestamp": str(row.get("timestamp")),
                "event_type": row.get("event_type"),
                "name": row.get("name"),
                "level": row.get("level"),
                "summary": row.get("summary"),
                "payload": payload_text,
            })

        # Усечение с гарантией валидного JSON (агент должен распарсить ответ):
        # 1) каждое событие — по отдельности; 2) отбрасываем самые старые
        #    события; 3) если даже одно не влезает (экстремальный лимит) —
        #    сжимаем payload, а при нехватке места — обнуляем его.
        def _render(items: list[dict], trunc: bool) -> str:
            return json.dumps(
                {
                    "status": "success",
                    "count": len(items),
                    "session_scope": "all" if allow_all else "current",
                    "truncated": trunc,
                    "events": items,
                },
                ensure_ascii=False,
                default=str,
            )

        per_event_cap = 4000
        for ev in events:
            if ev["payload"] and len(ev["payload"]) > per_event_cap:
                ev["payload"] = truncate_middle(ev["payload"], per_event_cap)

        truncated = False
        cap = per_event_cap
        while True:
            text = _render(events, truncated)
            if len(text) <= self.config.max_result_chars:
                break
            if len(events) > 1:
                events.pop()  # самое старое (список отсортирован DESC)
                truncated = True
                continue
            # остался один — уменьшаем cap payload'а, иначе обнуляем
            if cap > 16:
                cap //= 2
                if events[0]["payload"]:
                    events[0]["payload"] = truncate_middle(events[0]["payload"], cap)
                continue
            events[0]["payload"] = ""
            truncated = True
            text = _render(events, truncated)
            break

        return text

    def _error(self, error_type: str, message: str) -> str:
        return json.dumps(
            {"status": "error", "error_type": error_type, "message": message},
            ensure_ascii=False,
        )


def _current_session_key() -> str | None:
    try:
        from nanobot.agent.tools.context import current_request_session_key

        return current_request_session_key()
    except Exception:
        return None


def _log_table() -> tuple[str, str]:
    try:
        from config import SETTINGS

        db = (
            (SETTINGS.get("logging", {}) or {}).get("db", {}) or {}
        )
        schema = db.get("schema") or "public"
        table = db.get("table_name") or "agent_gateway_logs"
        return schema, table
    except Exception:
        return "public", "agent_gateway_logs"

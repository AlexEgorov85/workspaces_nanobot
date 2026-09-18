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

Контракт (см. ``openspec/specs/tools-history-search``):

  * **Scope isolation (security)**: ``session_scope="current"`` фильтрует
    по ``session_id`` текущего запроса (из RequestContext.session_key);
    ``session_scope="all"`` фильтрует по ``user_id`` текущего запроса
    (из RequestContext.sender_id). Это закрывает cross-user leakage:
    ``scope="all"`` возвращает только события того же пользователя, не
    глобальную выборку. При отсутствии identity для соответствующего
    scope tool возвращает структурированную ошибку (``missing_session_identity``
    / ``missing_user_identity``), и SQL-запрос НЕ выполняется.
  * **Identity source**: единственный — ``RequestContext`` из
    ``nanobot.agent.tools.context``. Имя поля фиксируется через приватный
    helper ``_current_user_id()`` (для ``scope="all"``) — никаких обращений
    к ``sender_id``/``session_id``/``chat_id``/``actor``/``payload`` из других
    мест. Это инкапсулирует зависимость от nanobot 0.3.0.
  * **Пагинация**: ``offset`` (целое ≥ 0, дефолт 0) пропускает первые
    ``offset`` строк после ``ORDER BY timestamp DESC, id DESC``. SQL
    запрашивает ``LIMIT effective_limit + 1`` строк; лишняя строка
    определяет ``db_has_more``.
  * **Детерминированный порядок**: tie-breaker по ``id`` (UUID) — стабильный
    для равных ``timestamp`` (типично при multi-row INSERT в одном батче).
  * **Раздельные truncation-флаги**: ``results_truncated`` (на ответе) —
    выброшены целые события из-за ``max_result_chars``;
    ``payload_truncated`` (на каждом событии) — payload ужат через
    ``truncate_middle``. ``truncated`` — deprecated алиас
    ``results_truncated``.
  * **Честный ``has_more``**: ``db_has_more OR results_truncated`` —
    композитная формула, чтобы следующая страница была видна даже когда
    ``LIMIT N+1`` не обнаружил следующей строки в БД, но часть
    отобранных событий была отброшена truncation'ом.
  * **next_offset**: ``offset + count`` — после truncation-проходов,
    чтобы продолжить пагинацию без пропуска отброшенных событий.
  * **Без утечки ``user_id``**: ``user_id`` НЕ возвращается в payload'е
    события и НЕ принимается как параметр tool'а — это внутренний
    security attribute.
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
        "offset": {
            "type": "integer",
            "description": (
                "Сколько первых событий пропустить в сортировке "
                "(пагинация). Дефолт 0 — первая страница. Продолжать "
                "страницы через поле next_offset из предыдущего ответа, "
                "НЕ через ``offset + limit`` (при results_truncated=true "
                "часть событий была отброшена, и арифметика offset+limit "
                "пропустит их)."
            ),
            "minimum": 0,
            "default": 0,
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
            "(since/until ISO-8601), session_scope ('current' default = "
            "current session_id; 'all' = all sessions of the current user; "
            "NEVER a global cross-user search). When identity-store is "
            "unavailable for the requested scope (e.g. outside a request), "
            "the tool returns a structured error and does NOT execute the "
            "SQL query (missing_session_identity / missing_user_identity). "
            "limit, offset (pagination; continue via next_offset from "
            "previous response, NOT offset+limit when results_truncated=true). "
            "Returns JSON {status, count, session_scope, has_more, "
            "next_offset, results_truncated, truncated (deprecated alias), "
            "events:[{event_id, timestamp, event_type, name, level, summary, "
            "payload, payload_truncated}]}; event_id — UUID строки "
            "agent_gateway_logs, payload — JSON-string. has_more=true если "
            "есть следующая страница (композитная формула db_has_more OR "
            "results_truncated). user_id is NEVER returned in the response "
            "(security boundary). After getting results: parse payload "
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
        offset: int | None = None,
        **_kwargs: Any,
    ) -> str:
        if session_scope not in ("current", "all"):
            return self._error(
                "invalid_session_scope",
                f"session_scope must be 'current' or 'all', got {session_scope!r}",
            )

        allow_all = session_scope == "all"

        if allow_all:
            user_id = _current_user_id()
            if not user_id:
                return self._error(
                    "missing_user_identity",
                    (
                        "session_scope='all' требует идентификатор пользователя "
                        "из текущего request context (RequestContext.sender_id); "
                        "identity-store недоступен или sender_id is None. "
                        "Без identity tool не выполняет SQL-запрос и не "
                        "возвращает чужие события."
                    ),
                )
        else:
            session_id = _current_session_key()
            if not session_id:
                return self._error(
                    "missing_session_identity",
                    (
                        "session_scope='current' требует ключ текущей сессии "
                        "из RequestContext.session_key; identity-store "
                        "недоступен."
                    ),
                )

        original_offset = max(0, int(offset or 0))
        effective_limit = min(int(limit or self.config.max_rows), self.config.max_rows)

        clauses: list[str] = []
        params: list[Any] = []

        if allow_all:
            # ``user_id = %s`` — единственный security boundary для
            # cross-session search. Без ``OR session_id = %s`` / ``WHERE TRUE``:
            # фильтрация строго по user_id. Если user_id NULL в БД —
            # строка не попадёт в выборку (безопасное поведение, см. V004).
            clauses.append("user_id = %s")
            params.append(user_id)
        else:
            clauses.append("session_id = %s")
            params.append(session_id)

        clauses.append("(%s IS NULL OR event_type = %s)")
        params.append(event_type)
        params.append(event_type)

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
        # Детерминированный порядок: ``timestamp DESC, id DESC`` — UUID как
        # tie-breaker защищает от потери/дублей строк одного батча flush'а
        # на границе страниц. ``LIMIT N+1`` даёт лишнюю строку для
        # детекции наличия следующей страницы в БД.
        sql = (
            f'SELECT id, "timestamp", event_type, name, level, summary, payload '
            f'FROM "{schema}"."{table}" '
            f"WHERE {' AND '.join(clauses)} "
            'ORDER BY "timestamp" DESC, "id" DESC '
            'LIMIT %s OFFSET %s'
        )
        params.append(effective_limit + 1)
        params.append(original_offset)

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
                "event_id": row.get("id"),
                "timestamp": str(row.get("timestamp")),
                "event_type": row.get("event_type"),
                "name": row.get("name"),
                "level": row.get("level"),
                "summary": row.get("summary"),
                "payload": payload_text,
            })

        # Phase 1 (до truncation): db_has_more определяется наличием
        # лишней строки в выборке. Лишняя строка отбрасывается из
        # ответа агента сразу — она служит только маркером.
        db_has_more = len(events) > effective_limit
        if db_has_more:
            events = events[:effective_limit]

        # Phase 2 (truncation): обрезаем payload каждого события до
        # ``per_event_cap`` символов через ``truncate_middle``; если общий
        # JSON не влезает в ``max_result_chars`` — отбрасываем самые
        # старые события (``events.pop()`` в порядке DESC), а затем при
        # необходимости уменьшаем ``cap`` (cap //= 2) и пережимаем
        # оставшийся payload.
        per_event_cap = 4000
        payload_truncated_flags: dict[int, bool] = {}

        for idx, ev in enumerate(events):
            if ev["payload"] and len(ev["payload"]) > per_event_cap:
                ev["payload"] = truncate_middle(ev["payload"], per_event_cap)
                payload_truncated_flags[idx] = True
            else:
                payload_truncated_flags[idx] = False

        results_truncated = False
        cap = per_event_cap

        def _render(items: list[dict], flags: dict[int, bool]) -> str:
            """Сериализовать ответ с финальным флагом ``payload_truncated``.

            ВАЖНО: ``payload_truncated`` включается в JSON при
            формировании, чтобы проверка ``len(text) <= max_result_chars``
            учитывала именно финальный размер ответа (включая доп.
            поле ``payload_truncated: bool``). Раньше ``_render``
            сериализовал события без этого флага, и проверка размера
            проходила по «промежуточному» JSON, а финальный ответ
            мог превысить ``max_result_chars`` на ~16-20 байт
            (``"payload_truncated": false`` на каждое событие).
            """
            decorated = [
                {**ev, "payload_truncated": bool(flags.get(idx, False))}
                for idx, ev in enumerate(items)
            ]
            return json.dumps(
                {
                    "status": "success",
                    "count": len(items),
                    "session_scope": "all" if allow_all else "current",
                    "events": decorated,
                    "results_truncated": False,
                    "has_more": False,
                    "next_offset": original_offset + len(items),
                    "truncated": False,
                },
                ensure_ascii=False,
                default=str,
            )

        while True:
            text = _render(events, payload_truncated_flags)
            if len(text) <= self.config.max_result_chars:
                break
            if len(events) > 1:
                # Самое старое событие в конце (DESC). Отбрасываем его.
                idx_dropped = len(events) - 1
                events.pop()
                payload_truncated_flags.pop(idx_dropped, None)
                results_truncated = True
                continue
            # Осталось одно событие — общий JSON всё ещё не влезает.
            # Уменьшаем cap и пережимаем payload (если ещё не пуст).
            if cap > 16:
                cap //= 2
                if events and events[0]["payload"]:
                    events[0]["payload"] = truncate_middle(events[0]["payload"], cap)
                    payload_truncated_flags[0] = True
                continue
            # Кап < 16 — обнуляем payload как последнее средство. Это
            # НЕ results_truncated (событие осталось в ответе); только
            # payload_truncated = true.
            if events:
                events[0]["payload"] = ""
                payload_truncated_flags[0] = True
            text = _render(events, payload_truncated_flags)
            break

        # ``has_more`` вычисляется ПОСЛЕ truncation-проходов: даже если
        # ``db_has_more = false`` (LIMIT N+1 не нашёл следующей строки в БД),
        # ``results_truncated = true`` означает, что часть отобранных
        # событий не показана агенту и следующая страница обязательна.
        has_more = bool(db_has_more or results_truncated)

        # ``payload_truncated`` уже добавлен в ``_render`` для каждого
        # события — теперь просто пересобираем ответ с финальными
        # флагами ``results_truncated`` / ``has_more`` / ``truncated``
        # (которые нельзя было вычислить ДО выхода из truncation-цикла).
        # ``next_offset`` — продолжить пагинацию через offset = count,
        # не через offset + limit (при results_truncated=true часть
        # событий была отброшена; offset + limit пропустил бы их).
        next_offset = original_offset + len(events)

        response = {
            "status": "success",
            "count": len(events),
            "session_scope": "all" if allow_all else "current",
            "has_more": has_more,
            "next_offset": int(next_offset),
            "results_truncated": bool(results_truncated),
            "truncated": bool(results_truncated),
            "events": [
                {
                    **ev,
                    "payload_truncated": bool(
                        payload_truncated_flags.get(idx, False)
                    ),
                }
                for idx, ev in enumerate(events)
            ],
        }
        return json.dumps(response, ensure_ascii=False, default=str)

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


def _current_user_id() -> str | None:
    """Получить идентификатор текущего пользователя из RequestContext.

    Единственная точка обращения к ``RequestContext.sender_id`` в
    history_search_tool. Инкапсулирует зависимость от nanobot 0.3.0:
    если в будущей версии поле будет переименовано, адаптация делается
    через эту функцию (см. contract-тест
    ``tests/contract/test_history_search_identity_contract.py``).

    Returns:
        ``str`` — если ``RequestContext`` доступен и ``sender_id`` задан;
        ``None`` — если контекста нет (вне оборота) или ``sender_id is None``.

    Никаких fallback'ов на другие поля (``session_id``, ``chat_id``,
    ``actor``, ``payload``) — отсутствие identity = жёсткий отказ.
    """
    try:
        from nanobot.agent.tools.context import current_request_context
    except Exception:
        return None
    try:
        ctx = current_request_context()
    except Exception:
        return None
    if ctx is None:
        return None
    sender_id = getattr(ctx, "sender_id", None)
    if isinstance(sender_id, str) and sender_id:
        return sender_id
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

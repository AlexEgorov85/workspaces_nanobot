"""``audit_analyzer_query`` — доступ агента к данным аудита через capability ``audit``.

Миграция ``enterprise-mcp-platform``, фаза 9. Навык ``audit_analyzer`` перестаёт
быть владельцем данных: он больше не открывает снимок, не строит SQL и не
держит собственный LLM-клиент. Данные обслуживает capability ``audit``
платформы, а агент ходит до неё через этот инструмент.

**Почему tool, а не операция напрямую.** ``MCPToolWrapper.execute`` нанобота
отправляет в вызов ровно те аргументы, которые задала модель, — то есть
идентичность пришла бы от модели. Единственный источник личности здесь —
``RequestContext``, поэтому операция вызывается из кода, а не из аргументов
модели (та же граница, что у ``history_search_tool``).

**Почему один инструмент, а не три.** Четыре операции различаются только
аргументами, а решение «какой режим» — это решение навыка, а не отдельная
возможность агента. Имя операции уезжает в сервер как есть: список ниже — это
ровно те имена, что в discovery.

Маршрутизация по ``operation``:

* ``list_scripts`` — каталог готовых скриптов с параметрами. **Зови первым**:
  он дешевле и предсказуемее сгенерированного запроса.
* ``run_script`` — выполнить готовый скрипт по имени из каталога.
* ``generate_sql`` — задачу обычной фразой; запрос строится и проверяется
  платформой, SQL писать не нужно.
* ``vector_search`` — семантический поиск по векторному индексу (capability
  ``vectors``). Для вопросов «найди похожие документы», а не для агрегатов.

Модель не пишет SQL и не выбирает таблицы: белый список таблиц и потолок строк
проверяются на платформе до выполнения, а текст запроса в ответе не
возвращается.

Конфиг читается из секции ``tools.audit_analyzer_query`` в ``config.json``
(через ``ctx._settings_ref.tools``, потому что pydantic-``ToolsConfig`` незнакомые
секции отбрасывает).
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from nanobot.agent.tools.base import Tool, tool_parameters
from pydantic import BaseModel, Field

from lib.services.enterprise_mcp_client import (
    CallIdentity,
    EnterpriseMcpUnavailable,
    EnterpriseOperationError,
)


def _cap(text: str, max_chars: int) -> str:
    """Обрезать так, чтобы потолок ДЕЙСТВИТЕЛЬНО держался.

    ``lib.utils.text_utils.truncate_middle`` объявляет ``max_chars`` жёстким
    потолком в докстринге, но на деле возвращает ``max_chars`` **плюс** маркер:
    при ``max_chars=500`` на выходе 535 символов. Проверено — существующий тест
    ``tests/test_text_utils.py::TestTruncateMiddle`` закрепляет именно это
    поведение (на ``max_chars=40`` он требует сохранить 4 символа головы и 4
    хвоста, что вместе с маркером не влезает в 40). Поэтому здесь обрезка
    своя: потолок — это потолок, иначе ``max_result_chars`` ничего не ограничивал
    бы. Когда под голову, маркер и хвост места нет — остаётся префикс.
    """
    marker = f"\n\n... ({len(text) - max_chars:,} chars truncated) ...\n\n"
    room = max_chars - len(marker)
    if room < 8:
        return text[:max_chars]
    head = room // 2
    tail = room - head
    return text[:head] + marker + text[len(text) - tail:]


#: Операции, до которых доходит этот tool. Значения совпадают с именами в
#: discovery — список берётся не из памяти модели, а из этого словаря, и
#: неизвестное значение отклоняется до сетевого вызова.
_ROUTED_OPERATIONS = ("list_scripts", "run_script", "generate_sql", "vector_search")


class AuditAnalyzerQueryToolConfig(BaseModel):
    """Конфиг секции ``tools.audit_analyzer_query`` в ``config.json``."""

    enable: bool = True
    max_result_chars: int = Field(default=20000, ge=500, le=200000)


@tool_parameters({
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": list(_ROUTED_OPERATIONS),
            "description": (
                "Какую операцию выполнить. 'list_scripts' — каталог готовых "
                "скриптов с их параметрами (зови первым, если задача решается "
                "готовым скриптом); 'run_script' — выполнить скрипт по имени "
                "из каталога; 'generate_sql' — ответить на вопрос обычной "
                "фразой, запрос построит и проверит платформа (предпочитай "
                "run_script, он дешевле и предсказуемее); 'vector_search' — "
                "семантический поиск по близким к запросу документам."
            ),
        },
        "script": {
            "type": "string",
            "description": (
                "Имя скрипта для operation='run_script' — ровно как оно в "
                "каталоге list_scripts (например 'analytics_by_year_month')."
            ),
        },
        "params": {
            "type": "object",
            "description": (
                "Параметры скрипта для operation='run_script': имена и значения "
                "из описания скрипта в каталоге. Не выдумывай имена — сначала "
                "вызови list_scripts."
            ),
        },
        "query": {
            "type": "string",
            "description": (
                "Текст запроса: для 'generate_sql' — вопрос обычной фразой "
                "(SQL писать не нужно); для 'vector_search' — что ищем по "
                "смыслу."
            ),
        },
        "index_name": {
            "type": "string",
            "description": (
                "Имя векторного индекса для 'vector_search'. Обязателен, если "
                "индексов несколько."
            ),
        },
        "top_k": {
            "type": "integer",
            "description": "Сколько результатов вернуть для 'vector_search' (по умолчанию 5).",
            "minimum": 1,
            "default": 5,
        },
        "threshold": {
            "type": "number",
            "description": (
                "Порог схожести для 'vector_search'. Не задан — без порога."
            ),
        },
    },
    "required": ["operation"],
})
class AuditAnalyzerQueryTool(Tool):
    """Ответить на вопрос по данным аудита (каталог скриптов, скрипт, NL→SQL, поиск)."""

    config_key: ClassVar[str] = "audit_analyzer_query"

    def __init__(
        self,
        *,
        config: AuditAnalyzerQueryToolConfig,
        client: Any = None,
        request_id_source: Any = None,
    ) -> None:
        self.config = config
        self._client = client
        #: Экземпляр ``DbLoggingService`` — источник ``request_id`` текущего
        #: оборота. Берётся из контекста, потому что индекс оборотов живёт в
        #: нём, а не в модуле: метод, а не функция.
        self._request_id_source = request_id_source

    @classmethod
    def config_cls(cls):
        return AuditAnalyzerQueryToolConfig

    @classmethod
    def _read_settings_section(cls, ctx: Any) -> dict[str, Any]:
        settings = getattr(ctx, "_settings_ref", None)
        if settings is None:
            return {}
        tools_section = getattr(settings, "tools", None)
        if tools_section is None:
            return {}
        section = getattr(tools_section, cls.config_key, None)
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
        return bool(cls._read_settings_section(ctx).get("enable", True))

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        section = cls._read_settings_section(ctx)
        try:
            config = cls.config_cls()(**section)
        except Exception:
            config = cls.config_cls()()
        # Клиент enterprise-mcp — единственный путь к данным аудита. ``None``:
        # раздел ``enterprise_mcp`` выключен или не задан (``config.json``). Tool
        # остаётся зарегистрированным и отвечает структурной ошибкой, чтобы
        # модель видела причину, а не «неизвестный инструмент».
        # ``_db_logging_service`` — с подчёркиванием: именно так его
        # проставляет ``project_tool_loader`` на ``ctx``. Раньше здесь было
        # ``db_logging_service``, и ``getattr`` молча возвращал ``None``:
        # ``request_id_source`` терял связь прогона с ``agent_question_runs``.
        return cls(
            config=config,
            client=getattr(ctx, "_enterprise_mcp", None),
            request_id_source=getattr(ctx, "_db_logging_service", None),
        )

    @property
    def name(self) -> str:
        return "audit_analyzer_query"

    @property
    def description(self) -> str:
        return (
            "Answer questions about audit data (аудиты, нарушения, отчёты) via "
            "the enterprise-mcp audit capability. Use INSTEAD of writing SQL "
            "yourself — you cannot: the table whitelist and row cap are checked "
            "on the platform before execution, and the SQL text is never "
            "returned. DECISION TREE: (1) call operation='list_scripts' first "
            "if a ready-made script may fit — it is cheaper and more "
            "predictable than generated SQL; (2) operation='run_script' with "
            "the script name from that catalog and its params; (3) "
            "operation='generate_sql' with a plain-language question when no "
            "script fits (a model builds and validates the query); (4) "
            "operation='vector_search' for 'find documents similar to X' — "
            "semantic retrieval, not aggregation. Script names and parameter "
            "names come ONLY from list_scripts — do not invent them. Returns "
            "the operation's JSON: {status, row_count, columns, rows, "
            "no_match?} for queries, the catalog with parameters/validation for "
            "list_scripts, and hits with scores for vector_search. If no_match "
            "is true, the data does not answer the question — say so instead of "
            "guessing; if the catalog is empty it is an error (registry_unavailable), "
            "not 'no scripts exist'."
        )

    async def execute(
        self,
        *,
        operation: str,
        script: str | None = None,
        params: dict[str, Any] | None = None,
        query: str | None = None,
        index_name: str | None = None,
        top_k: int = 5,
        threshold: float | None = None,
        **_kwargs: Any,
    ) -> str:
        if operation not in _ROUTED_OPERATIONS:
            return self._error(
                "invalid_operation",
                f"operation должен быть одним из {list(_ROUTED_OPERATIONS)}, "
                f"получено {operation!r}",
            )

        arguments = self._build_arguments(
            operation=operation,
            script=script,
            params=params,
            query=query,
            index_name=index_name,
            top_k=top_k,
            threshold=threshold,
        )
        if isinstance(arguments, str):
            return self._error("invalid_params", arguments)

        client = self._client
        if client is None:
            return self._error(
                "mcp_unavailable",
                "Клиент enterprise-mcp не создан: раздел enterprise_mcp выключен "
                "или не задан (config.json → gateway.agent.enterprise_mcp). "
                "Данные аудита доступны только через него.",
            )

        try:
            raw = await client.call(operation, arguments, identity=self._identity())
        except EnterpriseOperationError as exc:
            return self._error(exc.code, exc.message)
        except EnterpriseMcpUnavailable as exc:
            return self._error("mcp_unavailable", str(exc))
        except Exception as exc:  # noqa: BLE001 - модель не должна видеть traceback
            return self._error("unexpected_error", str(exc))

        if len(raw) <= self.config.max_result_chars:
            return raw
        return json.dumps(
            {
                "status": "success",
                "operation": operation,
                "truncated": True,
                "message": (
                    f"ответ длиннее {self.config.max_result_chars} символов и ужат; "
                    "сузьте запрос (меньше строк, конкретнее параметры) "
                    "и повторите"
                ),
                "preview": _cap(raw, self.config.max_result_chars),
            },
            ensure_ascii=False,
            default=str,
        )

    def _build_arguments(
        self,
        *,
        operation: str,
        script: str | None,
        params: dict[str, Any] | None,
        query: str | None,
        index_name: str | None,
        top_k: int,
        threshold: float | None,
    ) -> dict[str, Any] | str:
        """Собрать аргументы операции. Строка на входе — текст ошибки."""
        if operation == "list_scripts":
            return {}

        if operation == "run_script":
            if not script:
                return ("для operation='run_script' обязателен script — имя из "
                        "каталога list_scripts")
            arguments: dict[str, Any] = {"script": script}
            if params:
                arguments["params"] = params
            return arguments

        if operation == "generate_sql":
            if not query:
                return "для operation='generate_sql' обязателен query — вопрос обычной фразой"
            return {"query": query}

        # vector_search
        if not query:
            return "для operation='vector_search' обязателен query — что ищем по смыслу"
        arguments = {"query": query}
        if index_name:
            arguments["index_name"] = index_name
        arguments["top_k"] = int(top_k)
        if threshold is not None:
            arguments["threshold"] = float(threshold)
        return arguments

    def _error(self, error_type: str, message: str) -> str:
        return json.dumps(
            {"status": "error", "error_type": error_type, "message": message},
            ensure_ascii=False,
        )


    def _identity(self) -> CallIdentity | None:
        """Личность оборота — из ``RequestContext``, никогда из аргументов модели.

        Возвращает ``None`` вне оборота (нет ``RequestContext``): в этом случае
        сервер сам скажет ``identity_missing``, и придумывать значения здесь
        нельзя — выдуманный ``session_id`` выглядел бы как настоящая запись в
        журнале. Отсутствие журнала тоже не повод выдумывать ``request_id``.
        """
        try:
            from nanobot.agent.tools.context import (
                current_request_context,
                current_request_session_key,
            )
        except Exception:
            return None
        try:
            ctx = current_request_context()
        except Exception:
            return None
        if ctx is None:
            return None

        try:
            session_id = current_request_session_key()
        except Exception:
            session_id = None
        if not session_id:
            return None

        sender_id = getattr(ctx, "sender_id", None)
        user_id = sender_id if isinstance(sender_id, str) and sender_id else None
        if not user_id:
            return None

        request_id = None
        source = self._request_id_source
        if source is not None:
            try:
                request_id = source.get_request_id(str(session_id))
            except Exception:
                request_id = None

        return CallIdentity(
            session_id=str(session_id),
            user_id=user_id,
            request_id=request_id,
        )

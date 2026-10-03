"""``legal_summarizer_query`` — follow-up вопрос по сохранённой операции.

Регистрируется автоматически через
``lib/services/project_tool_loader.py::register_project_tools``.

Зачем: без этого tool'а агент на follow-up вопрос («сколько статей?», «какие
разделы?», «что в чанке 12?») вынужден перепарсить PDF заново.

Раньше этот вопрос уходил в ``cli_query.py``, поднятый **подпроцессом**:
интерпретатор запускался ради чтения JSON из уже разобранного документа.
Теперь домен живёт в платформе (change ``enterprise-mcp-platform``, фаза
11), короткий вопрос - это одна операция ``query_operation`` capability
``legal_summarizer``, и subprocess не нужен.

Конфиг читается из секции ``tools.legal_summarizer_query`` в ``config.json``
(через ``ctx._settings_ref.tools``, потому что pydantic-``ToolsConfig``
незнакомые секции отбрасывает).
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

#: Операция платформы, которую зовёт этот tool. Единственная: отдельного
#: каталога операций у capability нет, а выдумывать имена нельзя - неизвестное
#: имя отверг бы сам реестр.
_OPERATION = "query_operation"

#: Поля, которые принимает операция. Список берётся отсюда, а не из памяти
#: модели: неизвестное поле отвергнет схема операции.
_FIELDS = ("stats", "articles", "chunks", "sections", "tree", "all")


def _cap(text: str, max_chars: int) -> str:
    """Обрезать так, чтобы потолок ДЕЙСТВИТЕЛЬНО держался.

    Своя обрезка, а не ``truncate_middle``: тот возвращает ``max_chars`` плюс
    маркер, то есть потолок не является потолком. Когда под голову, маркер и
    хвост места нет - остаётся префикс.
    """
    marker = f"\n\n... ({len(text) - max_chars:,} chars truncated) ...\n\n"
    room = max_chars - len(marker)
    if room < 8:
        return text[:max_chars]
    head = room // 2
    return text[:head] + marker + text[-(room - head):]


class LegalSummarizerQueryToolConfig(BaseModel):
    """Конфиг секции ``tools.legal_summarizer_query`` в ``config.json``."""

    enable: bool = True
    max_result_chars: int = Field(default=20000, ge=500, le=200000)


@tool_parameters({
    "type": "object",
    "properties": {
        "operation_id": {
            "type": "string",
            "description": (
                "Идентификатор операции суммаризации. Обязателен: без него "
                "нечего спрашивать. Возвращается тем вызовом, который "
                "разбирал документ."
            ),
        },
        "field": {
            "type": "string",
            "enum": list(_FIELDS),
            "description": (
                "Что вернуть: stats (метрики и число статей) - по умолчанию; "
                "articles; chunks (сводки по чанкам); sections; tree "
                "(иерархия разделов); all (manifest целиком, крупный ответ)."
            ),
        },
        "max_chunk_summary_chars": {
            "type": "integer",
            "description": (
                "Обрезка текста сводки чанка для поля 'chunks'. "
                "По умолчанию 1500."
            ),
        },
    },
    "required": ["operation_id"],
})
class LegalSummarizerQueryTool(Tool):
    """Ответить follow-up вопросом по уже разобранному юридическому документу."""

    config_key: ClassVar[str] = "legal_summarizer_query"

    def __init__(
        self,
        *,
        config: LegalSummarizerQueryToolConfig,
        client: Any = None,
        request_id_source: Any = None,
    ) -> None:
        self.config = config
        self._client = client
        #: Экземпляр ``DbLoggingService`` - источник ``request_id`` текущего
        #: оборота. Берётся из контекста, потому что индекс оборотов живёт в
        #: нём, а не в модуле: метод, а не функция.
        self._request_id_source = request_id_source

    @classmethod
    def config_cls(cls):
        return LegalSummarizerQueryToolConfig

    @classmethod
    def _read_settings_section(cls, ctx: Any) -> dict[str, Any]:
        """Секция ``tools.legal_summarizer_query`` из настроек.

        pydantic-``ToolsConfig`` из nanobot знает только встроенные подсекции
        и неизвестные отбрасывает, поэтому читаем сырые настройки.
        """
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
            config = cls.config_cls()(
                enable=section.get("enable", True),
                max_result_chars=int(section.get("max_result_chars", 20000)),
            )
        except Exception:
            config = cls.config_cls()()
        # Клиент enterprise-mcp - единственный путь к состоянию операции.
        # ``None``: раздел ``enterprise_mcp`` выключен или не задан
        # (config.json → gateway.agent.enterprise_mcp). Tool остаётся зарегистрированным и отвечает
        # структурной ошибкой, чтобы модель видела причину, а не
        # «неизвестный инструмент».
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
        return "legal_summarizer_query"

    @property
    def description(self) -> str:
        return (
            "Follow-up вопрос по уже разобранному юридическому документу, по "
            "его operation_id: 'сколько статей?', 'какие разделы?', 'что в "
            "чанке 12?'. Зовёт операцию query_operation capability "
            "legal_summarizer платформы. Документ заново НЕ разбирается: "
            "ответ берётся из сохранённого состояния операции, поэтому "
            "вопрос дешёвый. Без operation_id вызов бессмысленен - его "
            "возвращает разбор документа. Поля: stats (по умолчанию), "
            "articles, chunks, sections, tree, all (крупный ответ, "
            "начинай с stats). Возвращает JSON операции."
        )

    async def execute(
        self,
        *,
        operation_id: str,
        field: str = "stats",
        max_chunk_summary_chars: int = 1500,
        **_kwargs: Any,
    ) -> str:
        if field not in _FIELDS:
            return self._error(
                "invalid_params",
                f"field должен быть одним из {list(_FIELDS)}, получено {field!r}",
            )

        client = self._client
        if client is None:
            return self._error(
                "mcp_unavailable",
                "Клиент enterprise-mcp не создан: раздел enterprise_mcp "
                "выключен или не задан (config.json → gateway.agent.enterprise_mcp). "
                "Состояние операции доступно только через него.",
            )

        arguments = {
            "operation_id": operation_id,
            "field": field,
            "max_chunk_summary_chars": max_chunk_summary_chars,
        }
        try:
            raw = await client.call(
                _OPERATION, arguments, identity=self._identity()
            )
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
                "operation": _OPERATION,
                "truncated": True,
                "message": (
                    f"ответ длиннее {self.config.max_result_chars} символов и "
                    "ужат; сузьте запрос (конкретнее поле, меньше "
                    "max_chunk_summary_chars) и повторите"
                ),
                "preview": _cap(raw, self.config.max_result_chars),
            },
            ensure_ascii=False,
            default=str,
        )

    def _error(self, error_type: str, message: str) -> str:
        return json.dumps(
            {"status": "error", "error_type": error_type, "message": message},
            ensure_ascii=False,
        )

    def _identity(self) -> CallIdentity | None:
        """Личность оборота - из ``RequestContext``, никогда из аргументов модели.

        Вне оборота возвращается ``None``: тогда сервер сам скажет
        ``identity_missing``. Придумывать значения здесь нельзя - выдуманный
        ``session_id`` выглядел бы как настоящая запись в журнале.
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

"""Контекст оборота: идентичность из ``params._meta`` плюс метаданные вызова.

Разделено на две части намеренно. :class:`McpCallContext` — это то, что пришло
по проводу в ``_meta``: его нельзя выдумать на сервере. :class:`ToolExecutionContext`
— это то, чем конвейер делится с доменом, журналом и файловым представлением:
идентичность плюс имя операции, время старта и свободные метаданные.

Смешивать их в один класс — значит через полгода получить в контексте пул, клиент
эмбеддингов и генератор отчётов: ``create_tool(container)`` станет вторым способом
получить зависимость, а проверка «в контексте только идентичность» перестанет быть
проверяемой. Службы операция берёт из ``ToolContainer``.

Форма ``_meta`` и её обязательность описаны в дельте ``runtime/call-contract``;
здесь только разбор и перенос в контекст. Разбор ровно один: конвейер читает
метаданные на границе и дальше передаёт объект, а не словарь.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

#: Префикс проектных ключей в ``_meta``. MCP резервирует
#: ``io.modelcontextprotocol/*`` под свой служебный обмен, поэтому голые имена без
#: префикса — шаг к коллизии с чужим расширением.
META_PREFIX = "workspaces/"
KEY_REQUEST_ID = f"{META_PREFIX}request_id"
KEY_SESSION_ID = f"{META_PREFIX}session_id"
KEY_USER_ID = f"{META_PREFIX}user_id"

#: Обязательные ключи. Отсутствие любого из них — нарушение контракта, а не
#: повод продолжить вызов без изоляции.
REQUIRED_KEYS: tuple[str, str, str] = (KEY_REQUEST_ID, KEY_SESSION_ID, KEY_USER_ID)

#: Имя служебного параметра, через который конвейер передаёт
#: :class:`ToolExecutionContext` операции.
#:
#: Живёт здесь, а не в ``registry``, по одной причине: ``registry`` импортирует
#: политики качества отсюда, и обратная ссылка образовала бы цикл импортов.
#: Слой исполнения — место, где контекст и появляется.
#:
#: Имя короткое и сокращённое не случайно. Имя-параметр обработчика живёт в одной
#: плоскости с доменными полями, и ``context`` для этого плохо: у
#: ``llm.complete`` так называется история диалога. Служебный параметр обязан
#: быть таким, чтобы операция не захотела занять его под данные.
CONTEXT_PARAM = "ctx"


class IdentityMissingError(Exception):
    """В вызове нет обязательной идентичности.

    Отдельный тип вместо доменного ``InvalidRequestError`` по одной причине: домен
    не может это исправить, это нарушение контракта на границе. Наружу уходит
    код ``identity_missing``.
    """

    code = "identity_missing"


@dataclass(frozen=True, slots=True)
class McpCallContext:
    """Идентичность вызова ровно та, что пришла в ``_meta``.

    Неизменяемая и без значений по умолчанию: подставить ``""`` вместо
    отсутствующего ``session_id`` — значит выполнить операцию без изоляции и
    выглядеть при этом успешно.
    """

    request_id: str
    session_id: str
    user_id: str

    @classmethod
    def from_meta(cls, meta: Mapping[str, Any] | None) -> McpCallContext:
        """Разобрать ``params._meta`` в идентичность вызова.

        Пустое значение, ``null`` и нестроковое значение одинаково считаются
        отсутствием: ключ есть, но воспользоваться им нельзя.
        """
        values: dict[str, str] = {}
        missing: list[str] = []
        for key in REQUIRED_KEYS:
            raw = (meta or {}).get(key)
            if raw is None or not str(raw).strip():
                missing.append(key)
                continue
            values[key] = str(raw).strip()
        if missing:
            raise IdentityMissingError(
                "в вызове нет обязательной идентичности: " + ", ".join(missing)
            )
        return cls(
            request_id=values[KEY_REQUEST_ID],
            session_id=values[KEY_SESSION_ID],
            user_id=values[KEY_USER_ID],
        )

    def as_meta(self) -> dict[str, str]:
        """Обратное преобразование — используется тестами и локальными вызовами."""
        return {
            KEY_REQUEST_ID: self.request_id,
            KEY_SESSION_ID: self.session_id,
            KEY_USER_ID: self.user_id,
        }


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Контекст одного исполнения операции.

    ``correlated`` отвечает на вопрос «ссылается ли ``request_id`` на существующий
    оборот в ``agent_question_runs``». Значение может отсутствовать в таблице и это
    не ошибка контракта, поэтому флагом, а не исключением.
    """

    call: McpCallContext
    tool_name: str
    capability: str
    started_at: datetime
    correlated: bool = True
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def request_id(self) -> str:
        return self.call.request_id

    @property
    def session_id(self) -> str:
        return self.call.session_id

    @property
    def user_id(self) -> str:
        return self.call.user_id

    @property
    def duration_ms(self) -> int:
        """Длительность на момент чтения — конвейер меряет свой, а не домен."""
        now = datetime.now(UTC)
        return max(0, int((now - self.started_at).total_seconds() * 1000))

    def with_metadata(self, **extra: Any) -> ToolExecutionContext:
        """Копия с дополненными метаданными.

        Контекст неизменяемый не из педантизма: журнал и файловая запись идут в
        разных шагах конвейера, и оба должны видеть один и тот же снимок.
        """
        merged = dict(self.metadata)
        merged.update(extra)
        return ToolExecutionContext(
            call=self.call,
            tool_name=self.tool_name,
            capability=self.capability,
            started_at=self.started_at,
            correlated=self.correlated,
            metadata=MappingProxyType(merged),
        )


def new_request_id() -> str:
    """`request_id` для вызова вне оборота.

    Создаёт его **агент**, а не сервер: подстановка на стороне сервера сделала бы
    корреляцию ненаблюдаемой — событие пришло бы с ключом, которого нет ни у
    одного оборота. Функция живёт здесь, потому что локальные вызовы (тесты,
    `mcp_tools/cli.py`) должны создавать его тем же способом, что и агент.
    """
    return f"local-{uuid.uuid4().hex}"

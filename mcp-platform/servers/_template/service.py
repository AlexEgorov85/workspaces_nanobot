"""Слой сервиса: вся бизнес-логика здесь.

Этот модуль обязан тестироваться БЕЗ MCP и БЕЗ агента. Если тест
на сервис требует поднять сервер или импортировать ``nanobot`` —
значит логика попала не туда.

Содержимое ниже — намеренно тривиальный пример формы, а не заготовка
реального домена. При копировании шаблона содержимое заменяется целиком,
структура сохраняется.
"""

from __future__ import annotations

from dataclasses import dataclass

from libs.enterprise_common.errors import InvalidRequestError, NotFoundError


@dataclass(frozen=True)
class EchoResult:
    """Доменный результат. Не MCP-тип и не dict — чтобы не протекали
    транспортные детали в логику и наоборот."""

    text: str
    length: int


class EchoService:
    """Минимальный сервис-демонстратор формы.

    Реальный сервис домена устроен так же: принимает примитивы,
    возвращает доменный тип, бросает ``EnterpriseError``-подклассы.
    Никаких ``ToolResult``, никакого сессионного состояния агента.
    """

    def echo(self, text: str) -> EchoResult:
        if not isinstance(text, str):
            raise InvalidRequestError("text must be a string")
        if not text.strip():
            raise InvalidRequestError("text must not be empty")
        return EchoResult(text=text, length=len(text))

    def find(self, key: str) -> EchoResult:
        """Пример доменной ошибки «не найдено» — а не транспортной."""
        raise NotFoundError(f"no entry for {key!r}")

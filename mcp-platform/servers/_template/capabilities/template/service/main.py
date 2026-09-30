"""Эталонный сервис: вся бизнес-логика здесь.

Намеренно тривиальный пример формы, а не заготовка реального домена. При
копировании содержимое заменяется целиком, структура сохраняется.

Сервис не знает про MCP: если тест на сервис требует поднять сервер, значит
логика попала не туда.
"""

from __future__ import annotations

from dataclasses import dataclass

from libs.enterprise_common.errors import InvalidRequestError, NotFoundError


@dataclass(frozen=True)
class EchoResult:
    """Доменный результат. Не MCP-тип и не dict — чтобы транспортные детали
    не протекали в логику и наоборот."""

    text: str
    length: int


class EchoService:
    """Минимальный сервис-демонстратор формы.

    Реальный сервис домена устроен так же: принимает примитивы, возвращает
    доменный тип, бросает ``EnterpriseError``-подклассы.
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

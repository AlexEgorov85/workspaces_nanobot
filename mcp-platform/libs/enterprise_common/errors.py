"""Общие ошибки enterprise-слоя.

Смысл: домен бросает свои исключения, MCP-адаптер маппит их на
``McpError``. Агент никогда не должен видеть ``psycopg2``/``duckdb``/
``faiss``-исключения — только осмысленный код и текст.
"""

from __future__ import annotations


class EnterpriseError(Exception):
    """Базовая ошибка enterprise-слоя. Транспортно-независима."""

    code = "enterprise_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class NotFoundError(EnterpriseError):
    """Запрошенный объект не найден."""

    code = "not_found"


class InvalidRequestError(EnterpriseError):
    """Некорректные аргументы запроса."""

    code = "invalid_request"


class InfrastructureError(EnterpriseError):
    """Сбой инфраструктуры: БД недоступна, файл кэша занят, индекс не собран.

    Это НЕ ошибка валидации. Агент должен понимать разницу: retry имеет
    смысл для InfrastructureError и бессмыслен для InvalidRequestError.
    """

    code = "infrastructure_error"

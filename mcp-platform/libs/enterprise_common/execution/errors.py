"""Нормализация отказа вызова в код и текст.

Смысл модуля один: агент не должен получать ``KeyError: 'rows'`` или текст
psycopg2. Он получает код из закрытого списка и сообщение, по которому видно,
что делать — исправить запрос, повторить или не повторять.

Новые коды ради слоя исполнения не заводятся: отказы конвейера укладываются в
словарь из `docs/MCP-CONTRACTS.md` §2. Новый код означал бы, что вызывающему
придётся различать «слой исполнения» и «домен», а он не знает, какой из них
отказал.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..errors import EnterpriseError
from ..session.security import PathDeniedError
from .context import IdentityMissingError

#: Коды, которые могут уйти в ответе операции. Список закрыт: агент разбирает
#: его по веткам, и молчаливый новый код он обработает как «неизвестно».
FAILURE_CODES: frozenset[str] = frozenset(
    {
        "invalid_params",
        "invalid_request",
        "not_found",
        "no_match",
        "not_answerable",
        "registry_unavailable",
        "identity_missing",
        "timeout",
        "upstream_unavailable",
        "index_build_failed",
        "queue_full",
        "internal",
        "infrastructure_error",
    }
)

#: Коды, при которых повтор того же вызова осмыслен. Остальные повторять
#: бессмысленно: те же аргументы дадут тот же отказ.
RETRYABLE_CODES: frozenset[str] = frozenset(
    {"timeout", "infrastructure_error", "upstream_unavailable", "index_build_failed", "queue_full"}
)

#: Сообщения внутренних отказов, которые агенту показывать нельзя.
_INTERNAL_HINT = "внутренняя ошибка платформы"


class ExecutionTimeout(Exception):
    """Срок исполнения операции истёк.

    Отдельный тип по одной причине: ``concurrent.futures`` и ``anyio`` уже
    бросают свои ``TimeoutError``, и по классу нельзя отличить «операция не
    уложилась» от «сломался планировщик». Внутри наружу различать нечего, наружу
    оба дают код ``timeout``.
    """

    code = "timeout"


@dataclass(frozen=True, slots=True)
class Failure:
    """Отказ в форме, в которой его видит вызывающий."""

    code: str
    message: str
    retryable: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "retryable": self.retryable}}


def _failure(code: str, message: str) -> Failure:
    return Failure(code=code, message=message, retryable=code in RETRYABLE_CODES)


def normalize_exception(exc: BaseException) -> Failure:
    """Перевести исключение домена или платформы в ``Failure``.

    Доменный ``EnterpriseError`` сохраняет свой код: способность различает
    ``not_found`` и ``no_match`` осмысленно, и приводить их к одному коду —
    значит стереть эту разницу.

    Порядок проверок значим: ``FileNotFoundError`` — подкласс ``OSError``,
    ``IdentityMissingError`` — подкласс ``Exception``, а не ``EnterpriseError``,
    поэтому «широкие» ветки обязаны идти после узких.
    """
    if isinstance(exc, ExecutionTimeout):
        return _failure("timeout", str(exc) or "операция не уложилась в отведённое время")
    if isinstance(exc, IdentityMissingError):
        return _failure(exc.code, str(exc))
    if isinstance(exc, PathDeniedError):
        return _failure("invalid_params", str(exc))
    if isinstance(exc, EnterpriseError):
        return _failure(exc.code, str(exc))
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return _failure("timeout", "операция не уложилась в отведённое время")
    if isinstance(exc, FileNotFoundError):
        return _failure("not_found", str(exc) or "файл не найден")
    if isinstance(exc, PermissionError):
        return _failure("infrastructure_error", "нет прав на операцию с файлом")
    if isinstance(exc, OSError):
        return _failure("infrastructure_error", f"ошибка ввода-вывода: {exc}")
    if isinstance(exc, (ValueError, TypeError)):
        # Аргументы пришли не те: это вина вызывающего, и код должен его
        # направить исправить запрос, а не повторять.
        return _failure("invalid_params", str(exc) or "неверные аргументы")
    # Дальше — наш собственный дефект. Тип и текст исключения наружу не уходят:
    # они не помогают вызывающему и раскрывают устройство платформы.
    return _failure("internal", _INTERNAL_HINT)


def failure_from_payload(payload: Any) -> Failure | None:
    """Взять отказ из тела результата capability.

    Часть операций сигналит об отказе возвратом ``{"error": {...}}`` без
    исключения: там, где отказ — обычный исход ветки (например «нет такой
    задачи»), а не сбой. Конвейер обязан такой отказ увидеть, иначе он уедет
    в ответе как успех.
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("error")
    if not isinstance(raw, dict):
        return None
    code = str(raw.get("code") or "").strip()
    if code not in FAILURE_CODES:
        code = "internal"
    message = str(raw.get("message") or "").strip() or _INTERNAL_HINT
    return _failure(code, message)

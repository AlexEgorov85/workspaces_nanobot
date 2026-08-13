"""Универсальный retry с exponential backoff.

Единая реализация повтора вызова при перечисленных исключениях —
используется для БД-запросов, HTTP-вызовов и т.п., где раньше каждый
модуль писал свой цикл ``for attempt ... time.sleep(delay * 2)``.

Паттерн «retry-и-пере-raise»: после ``max_retries`` неудач последнее
исключение пробрасывается наружу. Варианты с иной семантикой
(вернуть default при всех ошибках; печатать в stderr; разная обработка
конкретных кодов) — целевые реализации, их унификация меняет поведение.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def retry_on_exception(
    fn: Callable[[], T],
    *,
    exceptions: Tuple[type, ...],
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 15.0,
    label: str = "retry",
    on_retry: Optional[Callable[[int, int, float, Exception], Optional[float]]] = None,
) -> T:
    """Повторить ``fn`` при ошибках из ``exceptions`` с exponential backoff.

    Args:
        fn: вызываемый объект без аргументов.
        exceptions: кортеж исключений, при которых повторяем.
        max_retries: максимум попыток (после последней пробрасывается).
        base_delay: начальная задержка перед первым повтором, сек.
        max_delay: потолок задержки (backoff удваивается), сек.
        label: имя операции для логов.
        on_retry: опциональный хук ``(attempt, max_retries, delay, exc)``.
            Если возвращает число — используется как задержка перед повтором
            вместо стандартной (удвоенной) ``delay``.

    Returns:
        Результат ``fn()``.
    """
    delay = base_delay
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except exceptions as e:
            if attempt >= max_retries:
                raise
            if on_retry is not None:
                custom = on_retry(attempt, max_retries, delay, e)
                if custom is not None:
                    delay = custom
            logger.warning(
                "%s retry %d/%d after %.1fs: %s",
                label, attempt, max_retries, delay, e,
            )
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
    # Недостижимо (последняя попытка пробрасывает исключение), но явно
    # возвращаем результат fn(), чтобы тип был корректным и mypy был доволен.
    return fn()
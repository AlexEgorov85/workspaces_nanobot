"""Универсальный retry с exponential backoff.

Портировано из агента ``lib/utils/retry.py`` **без изменения поведения**: тот
же «retry-и-пере-raise», те же дефолты (``max_retries=3``, ``base_delay=1.0``,
``max_delay=15.0``), тот же хук ``on_retry`` и тот же текст предупреждения.

Живёт в ``enterprise_common``, а не в ``libs/llm``: повтор нужен не только
LLM-вызову, и второе определение в пакете-владельце означало бы, что его
придётся дублировать в каждом следующем владельце разделяемого ресурса.

**Определение одно.** Агентская копия ``lib/utils/retry.py`` удалена
2026-10-01 вместе с ``lib/services/cache_provider_impl.py``, который был её
единственным импортером; других импортов в агенте не осталось. Пункт 3.14
плана закрыт, блокировка «до фазы 9» снята.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def retry_on_exception(
    fn: Callable[[], T],
    *,
    exceptions: tuple[type, ...],
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 15.0,
    label: str = "retry",
    on_retry: Callable[[int, int, float, Exception], float | None] | None = None,
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

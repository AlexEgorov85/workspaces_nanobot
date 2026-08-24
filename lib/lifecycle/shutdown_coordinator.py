"""ShutdownCoordinator — упорядоченный graceful shutdown сервисов.

Регистрирует экземпляры по имени, останавливает в обратном порядке
регистрации (LIFO). Это гарантирует, что зависимости остановлены ПОСЛЕ
своих потребителей: каналы → канал-менеджер → шина → аудит-сервисы →
агент → сессии на диск.

Типичный порядок регистрации в ``ApplicationContext.start()``:

    1. db_logging_service      (записывает остаточные события)
    2. audit_sync_service      (читает из БД — должен жить дольше writer'а)
    3. ...

Все stop-функции вызываются с ``try/except`` — ошибка одной НЕ
блокирует остановку остальных (иначе битый сервис мог бы оставить
процесс висящим).

КРИТИЧНО: компонент регистрируется ОДИН раз. Повторный ``register()``
того же объекта приведёт к двойному stop (не критично, но лишний
шум). Используйте ``clear()`` только при полной переинициализации
(например, в тестах).

Какие компоненты поддерживаются:
  * любой объект с методом ``close()`` / ``stop()`` / ``shutdown()`` /
    ``terminate()`` (берётся первый найденный);
  * обычная функция ``def() -> None`` — будет вызвана напрямую.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class ShutdownCoordinator:
    """Регистр компонентов для упорядоченной остановки (LIFO).

    Attributes:
        _components: список пар ``(name, stop_fn)`` в порядке
            регистрации. ``shutdown_all()`` обходит его ``reversed()``.
    """

    def __init__(self) -> None:
        self._components: list[tuple[str, Callable[[], None]]] = []

    def register(self, name: str, component: Any) -> None:
        """Зарегистрировать компонент для последующей остановки.

        Допустимо:
          * объект с методом ``close()`` / ``stop()`` / ``shutdown()`` /
            ``terminate()`` (первый найденный через ``hasattr`` берётся);
          * обычная функция ``def() -> None`` (вызывается напрямую).

        Args:
            name: человекочитаемое имя для логов (например,
                ``"db_logging_service"``). Должно быть уникальным.
            component: сам компонент (см. выше).

        Raises:
            TypeError: если у объекта нет подходящего метода.
        """
        self._components.append((name, _resolve_stop_fn(component)))

    def shutdown_all(self) -> None:
        """Остановить компоненты в обратном порядке (LIFO).

        Исключения каждого компонента глотаются и логируются через
        ``logger.warning`` — остановка остальных продолжается. Это
        важно: один зависший сервис не должен оставить процесс
        висящим навечно.
        """
        for name, stop_fn in reversed(self._components):
            try:
                stop_fn()
            except Exception as exc:
                logger.warning("Error stopping %s: %s", name, exc)

    def clear(self) -> None:
        """Очистить реестр. Используется только в тестах."""
        self._components.clear()


def _resolve_stop_fn(component: Any) -> Callable[[], None]:
    """Найти подходящий stop-метод у компонента.

    Порядок поиска: ``close`` → ``stop`` → ``shutdown`` → ``terminate``.
    Если ни один не найден — ``TypeError`` (это ошибка регистрации,
    лучше упасть сразу, чем молча игнорировать).

    Args:
        component: объект или функция.

    Returns:
        Callable, вызов которого останавливает компонент.

    Raises:
        TypeError: у объекта нет ни одного из четырёх методов.

    Note:
        ``MagicMock()`` тоже callable, поэтому НЕ используем
        ``callable(component)`` — иначе ``MagicMock().close()`` не
        вызывался бы. Используем ``inspect.isfunction``/``isbuiltin``
        для определения «это просто функция».
    """
    import inspect

    if inspect.isfunction(component) or inspect.isbuiltin(component):
        return component
    for attr in ("close", "stop", "shutdown", "terminate"):
        if hasattr(component, attr):
            method = getattr(component, attr)
            if callable(method):
                return method
    raise TypeError(
        f"Cannot resolve stop function for {component!r}: "
        "no method among (close, stop, shutdown, terminate)"
    )

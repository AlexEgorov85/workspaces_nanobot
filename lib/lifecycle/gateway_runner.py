"""GatewayRunner — главный цикл gateway с перезапуском (exponential backoff).

При необработанном исключении gateway перезапускается с увеличивающейся
паузой (1s → 2s → 4s → 8s → 16s → 30s). Чистое завершение (clean shutdown)
выходит из цикла без перезапуска.

Семантика:
  * ``run_once()`` бросила исключение → логируем, ``sleep(delay)``,
    ``delay = min(delay * 2, max_delay)``, повторяем;
  * ``run_once()`` вернулась нормально → процесс завершается (clean
    shutdown);
  * ``KeyboardInterrupt`` (Ctrl+C) → тихий выход без перезапуска;
  * backoff растёт только при падениях. После clean shutdown состояние
    нерелевантно (процесс завершается).

Зачем нужен backoff, а не мгновенный рестарт:
  * БД (Postgres, Redis) не успевают восстановиться — следующий
    рестарт снова упадёт на connection error;
  * Если gateway упал из-за бага в коде, без backoff'а логи
    заспамятся exception-traceback'ами за секунды;
  * ``max_delay=30s`` — компромисс между «быстро вернуться» и
    «не долбить упавшие зависимости».

Использование::

    runner = GatewayRunner()
    runner.run_forever(lambda: asyncio.run(_gateway_main_loop()))
"""

from __future__ import annotations

import logging
import time
import traceback
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class GatewayRunner:
    """Цикл перезапуска gateway с exponential backoff.

    Attributes:
        _initial_delay: пауза перед первым рестартом (сек).
        _max_delay: потолок паузы (сек). После 30с backoff больше не растёт.
        _sleep: инъекция ``time.sleep`` (для тестов — подменяется).
    """

    def __init__(
        self,
        *,
        initial_delay: float = 1.0,
        max_delay: float = 30.0,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        """Args:
            initial_delay: секунды перед первым рестартом после падения.
            max_delay: максимальная пауза между рестартами (сек).
            sleep: инъекция ``time.sleep`` — тесты подменяют на
                ``list.append``, чтобы не блокировать.
        """
        self._initial_delay = initial_delay
        self._max_delay = max_delay
        self._sleep = sleep or time.sleep

    def run_forever(self, run_once: Callable[[], None]) -> None:
        """Запускать ``run_once`` бесконечно, перезапуская при падениях.

        Управляющие сигналы:
          * ``run_once()`` бросает ``Exception`` → логируем traceback,
            ждём ``delay`` секунд, удваиваем ``delay`` (cap = ``max_delay``),
            повторяем.
          * ``run_once()`` возвращается нормально → выходим из цикла
            (clean shutdown).
          * ``run_once()`` бросает ``KeyboardInterrupt`` → логируем INFO
            и выходим (НЕ перезапускаем — это пользователь жмёт Ctrl+C).

        Args:
            run_once: callable без аргументов. Должен либо вернуться
                (clean shutdown), либо бросить исключение (restart).
        """
        delay = self._initial_delay
        while True:
            try:
                run_once()
                return  # clean shutdown
            except KeyboardInterrupt:
                logger.info("Gateway interrupted")
                return
            except Exception as exc:
                logger.warning(
                    "Gateway exited unexpectedly, restarting in %.1fs: %s\n%s",
                    delay, exc, traceback.format_exc(),
                )
                self._sleep(delay)
                delay = min(delay * 2, self._max_delay)

    def reset_backoff(self) -> float:
        """Вернуть начальную задержку. Используется в тестах и при
        явном сбросе backoff после успешного цикла."""
        return self._initial_delay

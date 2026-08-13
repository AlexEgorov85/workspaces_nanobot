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

from config import get_setting

logger = logging.getLogger(__name__)


class GatewayRunner:
    """Цикл перезапуска gateway с exponential backoff.

    Attributes:
        _initial_delay: пауза перед первым рестартом (сек).
        _max_delay: потолок паузы (сек). После _max_delay backoff больше не растёт.
        _sleep: инъекция ``time.sleep`` (для тестов — подменяется).
    """

    def __init__(
        self,
        *,
        initial_delay: Optional[float] = None,
        max_delay: Optional[float] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        """Args:
            initial_delay: секунды перед первым рестартом после падения.
                По умолчанию — ``gateway.restart_initial_delay_sec`` (1.0).
            max_delay: максимальная пауза между рестартами (сек).
                По умолчанию — ``gateway.restart_max_delay_sec`` (30.0).
            sleep: инъекция ``time.sleep`` — тесты подменяют на
                ``list.append``, чтобы не блокировать.
        """
        self._initial_delay = float(
            initial_delay
            if initial_delay is not None
            else get_setting("gateway", "restart_initial_delay_sec", default=1.0)
        )
        self._max_delay = float(
            max_delay
            if max_delay is not None
            else get_setting("gateway", "restart_max_delay_sec", default=30.0)
        )
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

"""Наблюдение за живостью процесса платформы.

Что не так без этого модуля
---------------------------
Шлюз поднимает ``enterprise-mcp`` рукопожатием на старте и дальше обращается к
нему лениво: журнал пишется операцией ``log_events``, очередь обслуживается
операцией ``claim_task``, зеркало — своими тремя. Обрыв процесса замечает тот,
кто обращается: первая неудачная операция зовёт ``_reset()`` и поднимает
``EnterpriseMcpUnavailable``.

Проблема не в отказе, а в его форме. Пока никто не звал платформу, остановленный
процесс не даёт о себе знать ничего: объект сессии остаётся живым, readiness
остаётся ``READY``, журнал не содержит ни одной строки о поломке. Агент при этом
работает — и выглядит исправным. Второй симптом того же: восстановление
происходило только по запросу, то есть требовало чьего-то оборота.

Что делает наблюдатель
----------------------
* **Периодическая проба.** Протокольный ``ping`` — не бизнес-операция: он не
  трогает PostgreSQL и ничего не меняет, поэтому следить можно хоть каждые пол-
  минуты.
* **Лечение вместе с измерением.** Проба не только спрашивает, но и
  поднимает сессию, если её нет. Восстановление не ждёт чужого оборота, а
  происходит на ближайшей пробе.
* **Событие только на смену состояния.** Пока платформа лежит, неудачных проб
  будет много; шум из них делает недоступность невидимой ровно так же, как её
  отсутствие.
* **Наблюдаемость без ложной свежести.** Снимок состояния отдаётся тем, кому он
  нужен, — проверке готовности и статистике. Проверка готовности не ходит в
  процесс: она синхронна, а проба асинхронна, поэтому она читает результат
  последней пробы и честно показывает, сколько ему лет.

Отдельная оговорка, которую нельзя пропустить
--------------------------------------------
Когда платформа недоступна, недоступен и журнал: он тоже ходит через неё. Поэтому
событие наблюдателя, помеченное как деградация, может не доехать до базы — на
этот случай у журнала есть локальный fallback. Основным сигналом является
строка лога наблюдателя, а не событие в журнале.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from loguru import logger

#: Интервал между пробами, если он не объявлен. Достаточно редко, чтобы фоновая
#: подсистема не мешала работе, и достаточно часто, чтобы обрыв не оставался
#: незамеченным дольше нескольких минут.
DEFAULT_INTERVAL_SEC = 30.0

#: Событие деградации и восстановления. Имена — из общего словаря типов
#: событий платформы, а не придуманы здесь: неизвестный тип отвергается всей
#: партией, и отказ унёс бы с собой соседние записи.
EVENT_DEGRADED = "agent.degraded"
#: Отдельного ``agent.recovered`` в словаре нет, а выдумывать его здесь нельзя
#: по той же причине. Отчёт о восстановлении уходит как ``quality.check`` с
#: явным именем ``enterprise_mcp_health``: по факту это и есть проверка, и она
#: прошла. Если словарь когда-нибудь пополнится именем восстановления, заменить
#: нужно только эту константу.
EVENT_RECOVERED = "quality.check"

#: Уровни журнала — константами, а не литералами в вызовах, по образцу
#: ``MirrorPoller``: уровень, собранный на лету, выглядит правильно, а
#: ``CHECK valid_level`` в базе отвергает его и уносит весь батч.
LEVEL_WARN = "WARN"
LEVEL_INFO = "INFO"


@dataclass(frozen=True)
class McpHealthStatus:
    """Снимок наблюдения: последняя проба и её исход.

    ``up`` означает «последняя проба прошла», а не «процесс жив прямо
    сейчас»: между пробами проходит целый интервал, и лжи на этот счёт не
    должно быть даже в имени поля. За состояние сессии отвечает клиент
    (``health()``), за исход последней пробы — здесь.
    """

    up: bool
    checked_at: float | None
    error: str | None = None
    consecutive_failures: int = 0
    last_ok_at: float | None = None
    ever_checked: bool = False
    transition: str | None = field(default=None, compare=False)

    @property
    def age_sec(self) -> float | None:
        """Сколько секунд назад была последняя проба."""
        if self.checked_at is None:
            return None
        return max(0.0, time.time() - self.checked_at)

    def summary(self) -> str:
        """Строка для баннера и проверки готовности."""
        if not self.ever_checked:
            return "не проверялась"
        verdict = "UP" if self.up else "DOWN"
        age = self.age_sec
        age_text = f"{age:.0f}s назад" if age is not None else "возраст неизвестен"
        if self.up:
            return f"{verdict} (проба {age_text})"
        return f"{verdict} (проба {age_text}, подряд {self.consecutive_failures})"


#: Как состояние наблюдателя попадает в журнал. Сигнатура совпадает с той, что
#: принимает фабрика сборки: ``(event_type, name, payload, level)``.
PublishFn = Callable[[str, str, dict[str, Any], str], Awaitable[None]]

#: Служебная личность событий наблюдателя — та же, что у зеркала. Без неё
#: транспорт отправляет событие не в базу, а в локальный fallback и в счётчик
#: ``dropped``: отказ наблюдателя оказался бы невидим ровно тогда, когда он и
#: случается. Идентичность выбрана по образцу ``MirrorPoller``.
SERVICE_SESSION_ID = "mcp-health"
SERVICE_USER = "gateway"


class McpHealthMonitor:
    """Фоновая петля наблюдения за ``enterprise-mcp``.

    Не наследник ``MirrorPoller``: там общий с зеркалом ритм (цикл, дайджест,
    backoff на недоступность данных), а здесь наблюдение за процессом —
    фиксированный интервал и единственное действие «спросить, отвечает ли».
    Общего между ними ровно одно: отказ не убивает цикл.
    """

    def __init__(
        self,
        client: Any,
        *,
        interval_sec: float = DEFAULT_INTERVAL_SEC,
        publish: PublishFn | None = None,
    ) -> None:
        self._client = client
        self._interval = max(1.0, float(interval_sec))
        self._publish = publish
        self._status = McpHealthStatus(
            up=False, checked_at=None, ever_checked=False
        )
        self._task: asyncio.Task[None] | None = None
        self._probes = 0
        self._failures = 0
        self._recoveries = 0
        #: Доложена ли деградация. Именно он, а не предыдущая проба, решает,
        #: когда событие уместно: молчание после первой неудачной пробы сделало
        #: бы недоступность невидимой ровно так же, как её отсутствие.
        self._degradation_reported = False

    # -- состояние ---------------------------------------------------------

    def status(self) -> McpHealthStatus:
        """Текущий снимок наблюдения."""
        return self._status

    def get_stats(self) -> dict[str, Any]:
        """Счётчики для диагностики и операторской сводки."""
        status = self._status
        return {
            "server": getattr(self._client, "server_name", "enterprise-mcp"),
            "interval_sec": self._interval,
            "running": self._task is not None and not self._task.done(),
            "up": status.up,
            "ever_checked": status.ever_checked,
            "age_sec": status.age_sec,
            "consecutive_failures": status.consecutive_failures,
            "last_ok_at": status.last_ok_at,
            "last_error": status.error,
            "probes": self._probes,
            "failures": self._failures,
            "recoveries": self._recoveries,
        }

    # -- цикл --------------------------------------------------------------

    async def start(self) -> None:
        """Запустить петлю наблюдения."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run(), name="mcp-health-monitor")

    async def stop(self) -> None:
        """Остановить петлю и дождаться её выхода."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def run(self) -> None:
        """Петля: проба сразу, затем по интервалу. Отказ не завершает цикл."""
        while True:
            await self.check_once()
            await asyncio.sleep(self._interval)

    async def check_once(self) -> McpHealthStatus:
        """Одна проба с записью снимка и событием на смену состояния.

        Событие определяется явным флагом «деградация доложена», а не
        сравнением с предыдущей пробой. Разница видна на старте: первая проба
        может пройти (тогда молчим — ломки не было) или провалиться (тогда
        сообщаем — платформа не отвечает, даже если мы её ещё не видели
        живой). Сравнение с предыдущей пробой в первом случае объявило бы
        «восстановление» там, где ничего не восстанавливалось.
        """
        self._probes += 1
        previous = self._status
        try:
            await self._client.probe()
        except Exception as exc:  # noqa: BLE001 - любой отказ здесь равнозначен
            self._failures += 1
            self._status = McpHealthStatus(
                up=False,
                checked_at=time.time(),
                error=f"{type(exc).__name__}: {exc}",
                consecutive_failures=self._failures,
                last_ok_at=previous.last_ok_at,
                ever_checked=True,
                transition="down" if not self._degradation_reported else None,
            )
            if self._degradation_reported is False:
                self._degradation_reported = True
                logger.warning(
                    "enterprise-mcp: НЕ ОТВЕЧАЕТ — {} (неудачных проб подряд: {}). "
                    "Платформа поднимается заново на следующей пробе; до неё "
                    "журнал, очередь и зеркало недоступны.",
                    self._status.error,
                    self._failures,
                )
                await self._emit(
                    EVENT_DEGRADED,
                    "enterprise_mcp_unavailable",
                    {
                        "component": "enterprise_mcp",
                        "error": self._status.error,
                        "consecutive_failures": self._failures,
                    },
                    LEVEL_WARN,
                )
            return self._status

        if self._degradation_reported:
            self._degradation_reported = False
            self._recoveries += 1
            self._status = McpHealthStatus(
                up=True,
                checked_at=time.time(),
                error=None,
                consecutive_failures=0,
                last_ok_at=time.time(),
                ever_checked=True,
                transition="up",
            )
            logger.info(
                "enterprise-mcp: снова отвечает (неудачных проб подряд было: {})",
                self._failures,
            )
            await self._emit(
                EVENT_RECOVERED,
                "enterprise_mcp_recovered",
                {
                    "component": "enterprise_mcp",
                    "failures_total": self._failures,
                },
                LEVEL_INFO,
            )
            return self._status

        self._status = McpHealthStatus(
            up=True,
            checked_at=time.time(),
            error=None,
            consecutive_failures=0,
            last_ok_at=time.time(),
            ever_checked=True,
        )
        return self._status

    async def _emit(
        self,
        event_type: str,
        name: str,
        payload: dict[str, Any],
        level: str,
    ) -> None:
        """Отдать событие в журнал, если он есть.

        Отсутствие журнала или отказ публикации не должны ронять наблюдение:
        событие — сигнал для оператора, а не условие работы шлюза.
        """
        if self._publish is None:
            return
        try:
            # Уровень — keyword-only: так его дефолт остаётся каноническим
            # для стража уровней и не попадает в перебор имён событий, который
            # идёт по позиционным дефолтам.
            await self._publish(event_type, name, payload, level=level)
        except Exception as exc:  # noqa: BLE001 - сигнал не должен стоить цикла
            logger.debug("enterprise-mcp: событие не опубликовано: %s", exc)

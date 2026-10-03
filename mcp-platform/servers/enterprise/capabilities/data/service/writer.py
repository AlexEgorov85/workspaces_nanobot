"""Буфер записи журнала: неблокирующий вход для потока событий.

Зачем он отдельным классом, а не словарём в сервисе: логирование не должно
блокировать основной ход. Если запись события ждёт свободного воркера пула, то
запрос, который делает ``generate_sql`` с четырьмя вызовами LLM, начинает
конкурировать с журналом — и проигрывает, потому что потеря события безвозвратна,
а ошибки не несёт.

Отсюда правило: **переполнение — дроп, а не блокировка и не исключение.**
Событие журнала воспроизводимо (агент может спросить историю), событие
бизнес-данных — нет. Поэтому они ходят разными входами.

Петля логирования не замыкается: ошибка записи в БД не поднимается наружу и не
повторяется бесконечно — она попадает в счётчик, который виден в health-отчёте.

**Отказ пула — не ошибка записи.** Когда пул не даёт место, сброс откладывается:
батч возвращается в начало очереди, отказ считается отдельно, а следующий тик
повторяет попытку. Принцип «переполнение — дроп, а не блокировка» отвечает на
вход; отвечать им же на сам сброс было бы полпути: неблокирующий вход есть, а
запись — блокирующая, и при загруженном пуле именно она держит место дольше
всего. Отказ обратим, потеря — нет, поэтому и счётчики разные.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from libs.enterprise_common.settings import platform_settings

logger = logging.getLogger(__name__)

#: Событие не помещается — оно выбрасывается, счётчик растёт.
DROPPED = "dropped"

#: Как часто о потере событий говорится в лог. Счётчик в ``stats()`` растёт
#: всегда, а строка — не на каждое событие: переполнение идёт лавиной, и
#: по одной строке на запись лог превращается в шум, который никто не читает.
_DROP_LOG_EVERY = 1000

#: Как часто о затяжном занятии пула говорится в лог. Тот же довод, что и
#: выше: отказы идут каждый тик, и строка на отказ — это строка в минуту,
#: ничего не говорящая оператору. Первый отказ и каждый тысячный видимы,
#: точное число — в ``stats()``.
_BUSY_LOG_EVERY = 1000


class FlushDeferred(RuntimeError):
    """Сброс отложен: пул не дал место, батч возвращается в буфер.

    Именно поэтому это отдельное исключение, а не ``Exception``: отказ
    обратим, и обрабатывать его надо иначе, чем ошибку записи. Ошибка записи
    означает, что батч потерян (событие воспроизводимо только из истории
    агента), отказ — что батч уйдёт следующим тиком.

    Буфер не знает, какое именно исключение пула означает «занято»: знает
    владелец пула, и переводом на язык буфера занимается сервис.
    """


def batch_size_from_settings() -> int:
    """Размер батча сброса — из ``platform.json`` (``data.log_batch_size``).

    Раньше здесь стоял литерал ``256``, и вместе с ``data.log_flush_interval``
    он был невидимым потолком устойчивой пропускной способности журнала:
    сколько бы событий ни пришло, за окно сброса уходило не больше
    ``256 / flush_interval`` записей, а всё вышестоящее копилось в буфере
    до его переполнения. Литерал в коде нельзя было ни увидеть в конфигурации,
    ни поднять, не правя чужой файл, поэтому размер объявлен настройкой —
    единственным источником значения остаётся файл.
    """
    settings = platform_settings()
    return max(1, int(settings.get("ENTERPRISE_LOG_BATCH_SIZE")))


class EventBuffer:
    """Ограниченная очередь событий с фоновым сбросом.

    Параметры подобраны так, чтобы всплеск не съел память, а потеря событий
    оставалась заметной: ``maxlen`` держит объём, ``flush_interval`` — задержку,
    ``on_error`` — единственное место, где ошибка БД вообще становится видимой.
    """

    def __init__(
        self,
        flush: Callable[[list[dict[str, Any]]], None],
        *,
        maxlen: int = 2048,
        flush_interval: float = 5.0,
        batch_size: int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._flush = flush
        self._maxlen = max(1, int(maxlen))
        self._flush_interval = max(0.0, float(flush_interval))
        # ``None`` — не «взять дефолт писателя», а «взять из platform.json»:
        # у буфера нет права завести собственное число рядом с объявленным.
        self._batch_size = (
            max(1, int(batch_size)) if batch_size is not None else batch_size_from_settings()
        )
        self._clock = clock or time.monotonic
        self._queue: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_flush = self._clock()
        self._dropped = 0
        self._flush_errors = 0
        self._flush_rejected = 0
        self._written = 0

    # -- неблокирующий вход -------------------------------------------------

    def accept(self, event: dict[str, Any]) -> str | None:
        """Положить событие в буфер. Никогда не блокирует и не бросает.

        Возвращает ``None`` при успехе и ``DROPPED`` при переполнении, чтобы
        вызывающий мог посчитать потери, не ловя исключение на горячем пути.
        """
        with self._lock:
            if len(self._queue) >= self._maxlen:
                self._dropped += 1
                dropped = self._dropped
            else:
                dropped = 0
        if dropped:
            # Потеря события обязана быть видна без опроса счётчика: очередь
            # переполнилась — значит, сброс не поспевает за потоком, и это
            # видно только здесь. Порог и период сброса — в data.log_*
            # файла, а не в коде.
            if dropped == 1 or dropped % _DROP_LOG_EVERY == 0:
                logger.warning(
                    "буфер журнала переполнен: потеряно событий %d (потолок %d, "
                    "батч %d за %.1f с). Журнал теряет записи — поднимите "
                    "data.log_buffer_maxlen / data.log_batch_size или уменьшите "
                    "data.log_flush_interval",
                    dropped, self._maxlen, self._batch_size, self._flush_interval,
                )
            return DROPPED
        self._queue.append(event)
        self._wake.set()
        return None

    def accept_many(self, events: Iterable[dict[str, Any]]) -> int:
        dropped = 0
        for event in events:
            if self.accept(event) is not None:
                dropped += 1
        return dropped

    # -- фон ----------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="event-buffer", daemon=True)
        self._thread.start()

    def stop(self, *, drain: bool = True) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, self._flush_interval * 4))
            self._thread = None
        if drain:
            self.flush()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self._flush_interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            if self._clock() - self._last_flush >= self._flush_interval:
                self.flush()

    def flush(self) -> int:
        """Сбросить накопленное одним батчем. Возвращает число записанных.

        Три исхода, и их нельзя сливать:

        - **записано** — счётчик ``written`` растёт;
        - :class:`FlushDeferred` — пул занят. Батч возвращается в начало
          очереди целиком, растёт счётчик отказов, потери не происходит, и
          следующий тик повторяет попытку;
        - любая другая ошибка — запись не удалась, батч потерян. Повторять
          бесконечно тот же батч нельзя: это значит забить память событиями,
          которые никто не запишет. Событие журнала воспроизводимо (агент
          может спросить историю), потеря поэтому терпима, но обязана быть
          видна в счётчике.
        """
        with self._lock:
            if not self._queue:
                self._last_flush = self._clock()
                return 0
            batch = [self._queue.popleft() for _ in range(min(self._batch_size, len(self._queue)))]
        written = 0
        try:
            self._flush(batch)
        except FlushDeferred:
            self._put_back(batch)
            self._flush_rejected += 1
            rejected = self._flush_rejected
            if rejected == 1 or rejected % _BUSY_LOG_EVERY == 0:
                logger.warning(
                    "сброс буфера отложен, пул занят: отказов %d, событий в буфере "
                    "%d (батч %d вернулся в начало, потерь нет). Журнал копится, "
                    "пока пул занят",
                    rejected, self._pending(), len(batch),
                )
        except Exception as exc:  # noqa: BLE001 - ошибка БД не блокирует ход
            self._flush_errors += 1
            logger.warning("сброс буфера не удался, событий потеряно: %d: %s", len(batch), exc)
        else:
            self._written += len(batch)
            written = len(batch)
        with self._lock:
            self._last_flush = self._clock()
        # Число **записанных**, а не число попыток: иначе вызывающий, считающий
        # по возврату, записал бы в свой отчёт батч, который ушёл обратно в
        # буфер или потерялся.
        return written

    def _put_back(self, batch: list[dict[str, Any]]) -> None:
        """Вернуть батч в начало очереди.

        Порядок событий сохраняется: батч уходил с начала очереди и вернулся
        туда же, поэтому событие, принятое позже, не будет записано раньше
        него. Иначе журнал читался бы не в порядке событий, а в порядке
        попыток, а это разные вещи.

        Потолок при этом может оказаться превышен на размер батча — и это
        сознательно. Выбросить часть батча означало бы потерю событий, а
        потеря уже отсчитывается отдельным счётчиком и не должна
        подмешиваться в отказ.
        """
        with self._lock:
            self._queue.extendleft(reversed(batch))

    def _pending(self) -> int:
        with self._lock:
            return len(self._queue)

    # -- наблюдаемость ------------------------------------------------------

    def stats(self) -> dict[str, int]:
        with self._lock:
            pending = len(self._queue)
        return {
            "pending": pending,
            "dropped": self._dropped,
            "flush_errors": self._flush_errors,
            # Отказов, а не потерь: батч вернулся в буфер и уйдёт следующим
            # тиком. Считать его тем же числом, что и ``dropped``, нельзя —
            # по нему судят, теряется ли журнал, а он не теряется.
            "flush_rejected": self._flush_rejected,
            "written": self._written,
            "maxlen": self._maxlen,
            "batch_size": self._batch_size,
        }

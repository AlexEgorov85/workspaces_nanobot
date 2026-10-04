"""Механизм фонового зеркалирования: цикл, решения, отказы, метрики.

Модуль не знает, ЧТО зеркалится. Он знает, КАК это делается, и делает это сам;
конкретный ресурс объявляет ключи, умеет посчитать дайджест, прочитать
источник и собрать аргументы вызова.

Зачем базовый класс, а не копия на каждый ресурс
----------------------------------------------
Механизм — не тривиальная обвязка, а место, где живут все решения, которые
нельзя принимать на глаз у каждой новой копии:

* **один вызов состояния на цикл.** Без него синхронизация догоняла бы каждую
  единицу, чтобы узнать, что она не изменилась: на десяти единицах это десять
  походов в базу вместо одного;
* **дайджест, а не метка времени.** Метка времени у многих источников не
  поднимается при всех правках, и правило «зеркало не старше источника —
  пропустить» замирает навсегда, а починить разошедшееся зеркало будет нечем;
* **уборка не по первому пропуску.** Список присутствующих приходит извне, и
  пустой он бывает не «потому что всё удалили», а потому что источник
  недоступен: молчаливая уборка всего зеркала на таком сбое стоила бы месяцев
  переписки;
* **пустой список не стирает зеркало.** Это следствие предыдущего пункта, и
  оно неочевидно, поэтому зафиксировано отдельной защитой и отдельным
  счётчиком;
* **отказ платформы не убивает цикл.** Фоновая подсистема обязана пережить
  недоступность базы, а её отказ обязан быть виден — иначе она выглядит
  работающей ровно настолько, насколько её нельзя увидеть;
* **собственная личность вызова.** Зеркало работает вне оборота, личность
  оборота собрать не из чего, а сервер без неё отвечает ``identity_missing``.

Конкретный ресурс: ``lib/gateway/mirror/session_mirror.py``.

Граница платформы
-----------------
Механизм обращается к данным через операции платформы, а не через пул агента:
имена таблиц живут только на платформе, и вторая копия объявления разошлась
бы с первой при первой же смене настройки.
"""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from lib.services.db_logging_service import DbLoggingService
    from lib.services.enterprise_mcp_client import CallIdentity

#: Рост задержки при неудачах: base * 2**min(подряд идущих неудач, 5).
_BACKOFF_BASE_SEC = 1.0
_BACKOFF_CAP_SEC = 16 * 60.0

#: Пауза, после которой повторное сообщение о разошедшемся зеркале пишется
#: снова. Событие одно и то же из цикла в цикл, пока единица не починена; без
#: дедупликации журнал забивается одинаковыми строками.
_STALE_LOG_DEDUP_TTL = timedelta(seconds=60.0)

#: Уровни журнала — константами, а не литералами в вызовах. Собранный на лету
#: уровень неотличим от правильного по виду, но CHECK ``valid_level`` в базе
#: отвергает его и уносит весь батч, а не одну строку. Канон проверяется
#: ``tests/test_journal_level_canonical.py``.
_LEVEL_WARN = "WARN"
_LEVEL_INFO = "INFO"

#: Префикс сессии служебного вызова зеркала. Соглашение то же, что у воркера
#: очереди (``lib/channels/queue_ops.py``): фоновый вызов шлюза не имеет
#: оборота, и подписывать его чужой сессией нельзя — правка зеркала в журнале
#: выглядела бы как действие пользователя.
SERVICE_SESSION_PREFIX = "session-mirror"

#: Пользователь служебного вызова. Не существует как человек: обозначает сам
#: шлюз, а не того, кто что-то писал.
SERVICE_USER = "gateway"

#: Вердикты, при которых содержимое НЕ записывается. Всё остальное — запись.
#: Список назван явно, чтобы неизвестный вердикт не был молча принят за
#: «записали»: новый вердикт обязан заставить пересмотреть это место, а не тихо
#: продолжить работу по старой логике.
VERDICTS_WITHOUT_WRITE = frozenset({
    "unchanged",
    "skipped_equal",
    "skipped_within_tolerance",
    "skipped_stale",
})


def default_replica_id() -> str:
    """Идентичность реплики по умолчанию — имя машины.

    Именно машина, а не ``os.getpid()``: идентичность должна ПЕРЕЖИВАТЬ
    перезапуск. С ``pid`` реплика после перезапуска получила бы новое имя, её
    прежние строки остались бы в зеркале навсегда (они не её, очистка их не
    видит), и зеркало росло бы на мусоре после каждого рестарта.

    Несколько реплик на одной машине разводятся явной настройкой
    ``gateway.session_cold_sync.replica_id``.
    """
    return socket.gethostname() or "replica-unknown"


class MirrorEntry:
    """Единица ресурса, найденная в источнике: ключ плюс где его читать.

    ``locator`` — ресурс-зависимое (путь файла, идентификатор, координаты
    среза). Механизму он не нужен: он возвращается в ``digest_of`` и
    ``read_source`` без толкования.
    """

    __slots__ = ("key", "locator")

    def __init__(self, key: str, locator: Any = None) -> None:
        self.key = key
        self.locator = locator

    def __repr__(self) -> str:  # pragma: no cover - только для диагностики
        return f"MirrorEntry(key={self.key!r})"


class MirrorPoller:
    """Фоновое зеркалирование одного ресурса: источник → платформа.

    Наследник объявляет ресурс и четыре шага работы с ним; всё остальное —
    цикл, уборка, отказы, журнал и метрики — живёт здесь.

    Наследник НЕ переопределяет: ``_run``, ``_cycle``, ``_sync_changed``,
    ``_cleanup_absent``, ``_call``, ``_publish``, ``get_stats``. Переопределение
    любого из них означает вторую копию механизма, ради которой базовый класс и
    выделялся; если механизм не подходит ресурсу, это повод дописать его сюда,
    а не обойти.
    """

    #: Короткое имя ресурса: в тексте событий журнала и в сообщении об отказе.
    #: Не имя класса и не имя файла — по нему читатель ищет зеркало в журнале.
    resource_name: ClassVar[str] = "mirror"

    #: Операции платформы. Объявлены именами, а не разбросаны литералами по
    #: вызовам: сверка со списком зарегистрированных операций ловит случай,
    #: когда операция описана в коде, но отсутствует в реестре — вызов проходит
    #: чтение кода, компиляцию и все тесты с подставным клиентом, а падает в
    #: рантайме на каждом цикле. Проверка: ``tests/test_session_mirror_wire.py``.
    state_operation: ClassVar[str] = ""
    write_operation: ClassVar[str] = ""
    cleanup_operation: ClassVar[str] = ""

    #: Ключи в ответах платформы. Объявлены явно, потому что механизм читает
    #: ответ по имени, а не по позиции: переименование на стороне платформы
    #: должно ломать здесь громко, а не молча давать пустое зеркало.
    mirror_entries_field: ClassVar[str] = "sessions"
    mirror_count_field: ClassVar[str] = "count"
    deleted_count_field: ClassVar[str] = "deleted_sessions"
    deleted_keys_field: ClassVar[str] = "deleted_keys"

    def __init__(
        self,
        *,
        enterprise_mcp: Any | None = None,
        replica_id: str | None = None,
        enabled: bool = True,
        sync_interval_sec: float = 30.0,
        missing_cycles_threshold: int = 2,
        db_logging_service: DbLoggingService | None = None,
        task_name: str | None = None,
    ) -> None:
        self._mcp = enterprise_mcp
        self._replica_id = (replica_id or default_replica_id()).strip() or default_replica_id()
        self._sync_interval_sec = max(1.0, float(sync_interval_sec))
        self._missing_cycles_threshold = max(1, int(missing_cycles_threshold))
        self._db_logging = db_logging_service
        self._task_name = task_name or f"{self.resource_name}-poller"

        if self._mcp is None:
            self._enabled = False
            self._disabled_reason = "платформа недоступна (enterprise_mcp не задан)"
        else:
            self._enabled = bool(enabled)
            self._disabled_reason = "" if self._enabled else "выключено настройкой"

        self._task: asyncio.Task | None = None
        self._cycle_lock = asyncio.Lock()
        self._stopping = False

        self._cycles_total = 0
        self._cycles_failed_total = 0
        self._consecutive_failures = 0
        self._written_total = 0
        self._skipped_unchanged_total = 0
        self._unreadable_total = 0
        self._source_missing_total = 0
        self._cleanup_guarded_total = 0
        self._deleted_total = 0
        self._last_source_count = 0
        self._last_mirror_count = 0
        self._last_success_ts: float | None = None
        self._last_cycle_seconds: float | None = None

    # -- что объявляет наследник ----------------------------------------------

    def list_entries(self) -> list[MirrorEntry]:
        """Единицы ресурса, которые сейчас есть в источнике. Синхронно.

        Вызывается через ``asyncio.to_thread``: чтение каталога или хранилища
        блокирует, а event loop шлюза обслуживает ещё и обороты.
        """
        raise NotImplementedError

    def digest_of(self, entry: MirrorEntry) -> str | None:
        """Признак «единица не изменилась». ``None`` — прочитать не вышло.

        ``None`` обязан означать именно «сейчас нельзя решить», а не «не
        изменилось»: источник переписывается на лету, и объявленное «не
        изменилось» увело бы единицу из навсегда догоняемых.
        """
        raise NotImplementedError

    def read_source(self, key: str) -> Any:
        """Прочитать единицу целиком. ``None`` — источника для неё нет.

        Синхронно, вызывается через ``asyncio.to_thread``.
        """
        raise NotImplementedError

    def build_write_arguments(
        self, key: str, source: Any, digest: str
    ) -> dict[str, Any]:
        """Аргументы вызова записи одной единицы."""
        raise NotImplementedError

    def after_write(self, key: str, result: dict[str, Any]) -> None:
        """Разобрать вердикт платформы: счётчики и журнал.

        Вызывается только после успешного ответа. Счётчик ``written`` и
        «уборка не запускалась на пустом списке» считает механизм сам.
        """
        return None

    def guard_empty_source(self, present: list[str]) -> bool:
        """Запретить уборку на пустом списке источника.

        По умолчанию запрет включён. Наследник может его снять, только если его
        источник не может быть молча пустым (например, каталог создаётся заново
        при каждом запуске и «пусто» значит «удалено всё»).
        """
        return True

    def extra_stats(self) -> dict[str, Any]:
        """Ресурс-зависимые счётчики в ``get_stats()``."""
        return {}

    # -- свойства --------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def replica_id(self) -> str:
        return self._replica_id

    @property
    def disabled_reason(self) -> str:
        return self._disabled_reason

    # -- жизненный цикл --------------------------------------------------------

    async def start(self) -> None:
        """Запустить фоновую задачу. No-op, если зеркало выключено."""
        if not self._enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name=self._task_name)

    async def stop(self, timeout_sec: float = 30.0) -> None:
        """Остановить задачу, успев внести последние изменения.

        Финальный проход обязателен и идёт ДО закрытия источника: это последний
        шанс догнать то, что изменилось за время работы.
        """
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout_sec)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._task = None
        if not self._enabled:
            return
        try:
            await asyncio.wait_for(self._cycle(), timeout_sec)
        except asyncio.TimeoutError:
            self._log_failure(
                TimeoutError(f"финальный проход зеркала не уложился в {timeout_sec}с")
            )
        except Exception as exc:  # noqa: BLE001 - остановка не должна ронять выход
            self._log_failure(exc)

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - цикл не должен умирать
                self._consecutive_failures += 1
                self._cycles_failed_total += 1
                self._log_failure(exc)
            await asyncio.sleep(self._compute_delay())

    def _compute_delay(self) -> float:
        """Задержка до следующего прохода.

        Отказ от базы должен **отодвигать** следующую попытку, а не приближать
        её. Раньше здесь стоял ``min``, и при отказе цикл стучался чаще обычного
        (2 с против штатных 30) — то есть год отказа зеркала выглядел как
        усердная работа, а не как неработающая подсистема.

        Потолок ``_BACKOFF_CAP_SEC`` раньше был недостижим: показатель
        ограничивался пятёркой, и рост всегда упирался в 32 с. Теперь показатель
        растёт, пока не упрётся в потолок, — иначе длинная авария базы
        опрашивалась бы каждые полминуты вечно.
        """
        if self._consecutive_failures <= 0:
            return self._sync_interval_sec
        # Показатель ограничен: при 2**32 задержка уже заведомо выше потолка,
        # а считать целое на 100000 бит ради числа, которое срежется минимумом,
        # незачем.
        exponent = min(self._consecutive_failures, 32)
        backoff = _BACKOFF_BASE_SEC * (2 ** exponent)
        return min(max(self._sync_interval_sec, backoff), _BACKOFF_CAP_SEC)

    # -- цикл ------------------------------------------------------------------

    async def _cycle(self) -> None:
        """Один проход: состояние зеркала → догон изменённого → уборка."""
        async with self._cycle_lock:
            if not self._enabled:
                return
            self._cycles_total += 1
            started = asyncio.get_running_loop().time()
            try:
                present = await self._sync_changed()
                await self._cleanup_absent(present)
                self._consecutive_failures = 0
                self._last_success_ts = datetime.now().timestamp()
            finally:
                self._last_cycle_seconds = asyncio.get_running_loop().time() - started

    async def _sync_changed(self) -> list[str]:
        """Догнать изменившееся. Возвращает ключи, которые реально есть."""
        entries = await asyncio.to_thread(self.list_entries) or []
        by_key = {entry.key: entry for entry in entries}
        self._last_source_count = len(by_key)

        state = await self._call(self.state_operation, {"replica_id": self._replica_id})
        mirrored = state.get(self.mirror_entries_field) or {}
        self._last_mirror_count = int(state.get(self.mirror_count_field) or 0)

        for key in sorted(by_key):
            if self._stopping:
                return sorted(by_key)
            await self._sync_one(key, by_key[key], mirrored.get(key))
        return sorted(by_key)

    async def _sync_one(
        self, key: str, entry: MirrorEntry, cached: dict[str, Any] | None
    ) -> None:
        digest = await asyncio.to_thread(self.digest_of, entry)
        if digest is None:
            # Единица прямо сейчас переписывается либо недоступна. Не гадаем.
            self._unreadable_total += 1
            return

        if cached is not None and cached.get("source_digest") == digest:
            # Содержимое совпало с зеркалом. Это самый частый исход цикла, и он
            # не должен стоить ни чтения источника, ни обращения к данным: без
            # этой проверки каждый проход читал и разбирал всё целиком.
            self._skipped_unchanged_total += 1
            return

        source = await asyncio.to_thread(self.read_source, key)
        if source is None:
            self._source_missing_total += 1
            return

        result = await self._call(
            self.write_operation,
            self.build_write_arguments(key, source, digest),
        )
        verdict = str(result.get("verdict") or "")
        if verdict not in VERDICTS_WITHOUT_WRITE and verdict != "":
            self._written_total += 1
        self.after_write(key, result)

    async def _cleanup_absent(self, present: list[str]) -> None:
        """Убрать из зеркала единицы, которых больше нет в источнике."""
        if not present and self.guard_empty_source(present):
            if self._last_mirror_count:
                self._cleanup_guarded_total += 1
                self._publish(
                    "agent.degraded",
                    f"{self.resource_name}: список источника пуст, уборка "
                    f"пропущена — зеркало не тронуто",
                    level=_LEVEL_WARN,
                    payload={
                        "replica_id": self._replica_id,
                        self.mirror_count_field: self._last_mirror_count,
                    },
                )
            return

        result = await self._call(self.cleanup_operation, {
            "replica_id": self._replica_id,
            "present_keys": list(present),
            "delete_after_missed_cycles": self._missing_cycles_threshold,
        })
        deleted = int(result.get(self.deleted_count_field) or 0)
        if deleted:
            self._deleted_total += deleted
            for key in result.get(self.deleted_keys_field) or []:
                self._log_deleted(str(key))

    # -- обращение к платформе -------------------------------------------------

    def _service_identity(self) -> CallIdentity:
        """Личность служебного вызова: зеркало шлюза, а не пользователь.

        Реплика входит в имя сессии, а не теряется в ней: уборку и запись ведут
        несколько реплик, и в журнале они должны различаться — иначе следы двух
        машин выглядели бы как следы одной, а ``mirror`` чужой реплики нельзя
        отличить от своей. Имя ресурса в идентичность не входит: она перечисляет
        машины, а не то, что эта машина зеркалит, — ресурс назван в тексте
        события.
        """
        from lib.services.enterprise_mcp_client import CallIdentity

        return CallIdentity(
            session_id=f"{SERVICE_SESSION_PREFIX}:{self._replica_id}",
            user_id=SERVICE_USER,
        )

    async def _call(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Вызвать операцию платформы и разобрать её JSON-ответ.

        Вызов подписывается служебной личностью шлюза, а не личностью оборота:
        зеркало работает вне оборота, и без подписи сервер ответил бы отказом
        ``identity_missing``.

        Отказ платформы не проглатывается: он поднимается в цикл, который
        считает неудачу и откатывается назад по времени. Разбор ответа — с
        отказом по форме, а не ``json.loads`` в молчание: нечитаемый ответ хуже
        отсутствия ответа, потому что выглядит как пустой успех.
        """
        if not operation:
            raise RuntimeError(
                f"{type(self).__name__}: операция не объявлена — ресурс обязан "
                f"назвать свои операции платформы"
            )
        raw = await self._mcp.call(
            operation, arguments, identity=self._service_identity()
        )
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{operation}: ответ платформы не разобран как JSON ({exc})"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"{operation}: ответ платформы — {type(parsed).__name__}, "
                f"а должен быть объектом"
            )
        return parsed

    # -- журнал событий --------------------------------------------------------

    def _publish(
        self,
        event_type: str,
        summary: str,
        *,
        level: str = "WARN",
        session_id: str | None = None,
        user_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Отправить событие в журнал.

        Уровень пишется константами ``_LEVEL_WARN`` / ``_LEVEL_INFO`` на местах
        вызова, а дефолт параметра — литералом: ``CHECK valid_level`` в базе
        отвергает значение, которого в шкале нет, и отказ уносит весь батч, а
        не одну строку. Сборка уровня на лету (f-строка, ``.upper()``) выглядит
        правдоподобно и уезжает в базу неотличимо от правильного. Канон
        проверяет ``tests/test_journal_level_canonical.py``, включая требование
        дефолта: «WARN» по умолчанию, потому что деградация — это дефолтное
        состояние фоновой подсистемы, а не исключение.

        Личность события — служебная, та же, что у вызова зеркала. Без неё
        событие нечем подписать, транспорт отправляет его не в базу, а в
        локальный fallback и в счётчик ``dropped`` — то есть отказ фоновой
        подсистемы остаётся невидимым именно тогда, когда он и случается.
        Явные ``session_id``/``user_id`` переопределяют умолчание.
        """
        if self._db_logging is None:
            return
        from lib.services.db_logging_service import LogEvent, try_log_event

        identity = self._service_identity()
        try_log_event(
            self._db_logging,
            LogEvent(
                event_type=event_type,
                level=level,
                summary=summary,
                session_id=session_id or identity.session_id,
                user_id=user_id or identity.user_id,
                payload=payload or {},
            ),
            producer=type(self).__name__,
            event_type=event_type,
        )

    def _log_failure(self, exc: Exception) -> None:
        self._publish(
            "agent.degraded",
            f"{self.resource_name} cycle failed: {exc.__class__.__name__}",
            level=_LEVEL_WARN,
            payload={
                "error": str(exc)[:500],
                "consecutive_failures": self._consecutive_failures,
            },
        )

    def _log_deleted(self, key: str) -> None:
        self._publish(
            "agent.degraded",
            f"{self.resource_name}: удалена строка зеркала {key}",
            level=_LEVEL_INFO,
            session_id=key,
            payload={"key": key},
        )

    # -- метрики ---------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        last_success_lag = None
        if self._last_success_ts is not None:
            last_success_lag = max(
                0.0, datetime.now().timestamp() - self._last_success_ts,
            )
        stats: dict[str, Any] = {
            "resource": self.resource_name,
            "enabled": self._enabled,
            "disabled_reason": self._disabled_reason,
            "replica_id": self._replica_id,
            "cycles_total": self._cycles_total,
            "cycles_failed_total": self._cycles_failed_total,
            "consecutive_failures": self._consecutive_failures,
            "last_cycle_seconds": self._last_cycle_seconds,
            "last_success_ts": self._last_success_ts,
            "last_success_lag_seconds": last_success_lag,
            "source_count": self._last_source_count,
            "mirror_count": self._last_mirror_count,
            "written_total": self._written_total,
            "skipped_unchanged_total": self._skipped_unchanged_total,
            "unreadable_total": self._unreadable_total,
            "source_missing_total": self._source_missing_total,
            "cleanup_guarded_total": self._cleanup_guarded_total,
            "deleted_total": self._deleted_total,
            "sync_interval_sec": self._sync_interval_sec,
            "missing_cycles_threshold": self._missing_cycles_threshold,
        }
        stats.update(self.extra_stats())
        return stats

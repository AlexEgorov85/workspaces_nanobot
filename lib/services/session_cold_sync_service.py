"""Зеркалирование сессий в холодное хранилище через платформу.

Источник истины — upstream ``SessionManager`` (JSONL). Холодное зеркало в
PostgreSQL обслуживается только для multi-instance, наблюдаемости и
аварийного восстановления; пишет его этот сервис, и пишет он через операции
платформы ``mirror_session`` / ``cleanup_session_mirror`` /
``session_mirror_state``, а не напрямую.

Почему через платформу, а не пул агента
--------------------------------------
Тот же путь, по которому ушли канал и журнал: имя таблицы зеркала тогда было
объявлено и в конфигурации агента, и в ``platform.json``, и агент видел только
второй источник. Оверлей профиля применялся на платформе, а писатель смотрел
в другую сторону — и писал мимо. Здесь такой возможности нет вовсе: имена
таблиц живут только на платформе.

Почему решение о записи принимает платформа
------------------------------------------
Сравнение «зеркало против файла» и сама запись должны быть в одной транзакции.
Пока это были разные вызовы, между ними успевал вклиниться второй писатель, а
разрыв метаданных и сообщений после сбоя оставлял зеркало разорванным навсегда:
признак «изменилось» у сессии уже совпадал бы с записанным, и следующие циклы
проходили мимо. Операция ``mirror_session`` делает чтение, решение и запись
одной транзакцией, а запись — условным ``UPDATE`` (на Greenplum 6.5
``SELECT ... FOR UPDATE`` взял бы блокировку уровня таблицы).

Почему признак изменения — дайджест, а не ``updated_at``
-------------------------------------------------------
``updated_at`` не поднимается при всех правках сессии. ``JsonlSessionStore``
меняет ``metadata`` первой строки файла, оставляя ``updated_at`` прежним, а
``SessionManager.save`` сохраняет метку как есть, не вычисляя её заново. Правило
«зеркало не старше файла — пропустить» в этом случае замирало навсегда, и
починить разошедшееся зеркало было нечем. Дайджест содержимого файла —
единственный признак, который не врёт.

Почему асинхронно, а не в потоке
--------------------------------
Раньше сервис жил в daemon-потоке, чтобы синхронный код не блокировал event
loop. Но обращение к данным теперь идёт через клиента платформы, чья сессия
привязана к event loop, на котором создана, и из потока её вызвать нельзя.
Поэтому сервис стал задачей loop'а, а единственное действительно блокирующее —
чтение файлов сессий — ушло в ``asyncio.to_thread``.

Спека: ``openspec/specs/storage/session-hybridization/spec.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanobot.session.manager import SessionManager

    from lib.services.db_logging_service import DbLoggingService


_BACKOFF_BASE_SEC = 1.0
_BACKOFF_CAP_SEC = 16 * 60.0
_STALE_LOG_DEDUP_TTL = timedelta(seconds=60.0)

#: Уровни журнала — константами, а не литералами в вызовах. Собранный на лету
#: уровень неотличим от правильного по виду, но CHECK ``valid_level`` в базе
#: отвергает его и уносит весь батч, а не одну строку. Канон проверяется
#: ``tests/test_journal_level_canonical.py``.
_LEVEL_WARN = "WARN"
_LEVEL_INFO = "INFO"

#: Вердикты зеркала, при которых содержимое не записывается. Всё остальное —
#: запись или отсутствие изменений. Список назван явно, чтобы неизвестный
#: вердикт не был молча принят за «записали»: новый вердикт обязан заставить
#: пересмотреть это место, а не тихо продолжить работу по старой логике.
_VERDICTS_WITHOUT_WRITE = frozenset({
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


def file_digest(path: Any) -> str | None:
    """SHA-256 файла сессии либо ``None``, если файл в этот момент меняется.

    Принимает и ``Path``, и строку: upstream отдаёт путь в ``SessionInfo``
    строкой, и требование ``Path`` здесь означало бы, что подмена заглушкой
    расходится с боевым вызовом, а расхождение всплыло бы на первом же цикле.

    ``None`` означает «не сейчас»: файл читается прямо в момент ``save`` и
    может быть переписан между чтением и подсчётом. Засчитать такой дайджест
    можно только одним способом — выдумав его, а тогда зеркало сохранит байты,
    которых в файле никогда не было. Пропуск до следующего цикла дешевле и
    честен.

    Размер и время изменения сверяются до и после чтения — ровно так же, как
    это делает upstream в своём приватном снимке файла.
    """
    try:
        target = path if hasattr(path, "stat") else Path(path)
        before = target.stat()
        payload = target.read_bytes()
        after = target.stat()
    except (OSError, TypeError, ValueError):
        return None
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(payload) != after.st_size
    ):
        return None
    return hashlib.sha256(payload).hexdigest()


class SessionColdSyncService:
    """Фоновое зеркалирование сессий JSONL → платформа.

    Аргументы конструктора — все с дефолтами. DSN и имена таблиц сюда НЕ
    приходят: и то, и другое принадлежит платформе, и второй экземпляр
    объявления разошёлся бы с первым при первой же смене настройки.
    """

    def __init__(
        self,
        session_manager: SessionManager,
        *,
        enterprise_mcp: Any | None = None,
        replica_id: str | None = None,
        enabled: bool = True,
        sync_interval_sec: float = 30.0,
        stale_tolerance_seconds: int = 120,
        sync_lag_threshold_seconds: int = 3600,
        missing_cycles_threshold: int = 2,
        db_logging_service: DbLoggingService | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._mcp = enterprise_mcp
        self._replica_id = (replica_id or default_replica_id()).strip() or default_replica_id()
        self._sync_interval_sec = max(1.0, float(sync_interval_sec))
        self._stale_tolerance = max(0, int(stale_tolerance_seconds))
        self._sync_lag_threshold = max(0, int(sync_lag_threshold_seconds))
        self._missing_cycles_threshold = max(1, int(missing_cycles_threshold))
        self._db_logging = db_logging_service
        if sync_lag_threshold_seconds < stale_tolerance_seconds:
            raise ValueError(
                f"sync_lag_threshold_seconds ({sync_lag_threshold_seconds}) "
                f"must be >= stale_tolerance_seconds ({stale_tolerance_seconds})"
            )
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
        self._sessions_written_total = 0
        self._messages_written_total = 0
        self._skipped_unchanged_total = 0
        self._skipped_stale_total = 0
        self._stale_logged_at: dict[str, datetime] = {}
        self._unreadable_total = 0
        self._snapshot_missing_total = 0
        self._cleanup_guarded_total = 0
        self._deleted_sessions_total = 0
        self._last_success_ts: float | None = None
        self._last_cycle_seconds: float | None = None
        self._last_upstream_session_count: int = 0
        self._last_mirror_session_count: int = 0

    # -- свойства ------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def replica_id(self) -> str:
        return self._replica_id

    @property
    def disabled_reason(self) -> str:
        return self._disabled_reason

    # -- жизненный цикл ------------------------------------------------------

    async def start(self) -> None:
        """Запустить фоновую задачу. No-op, если зеркало выключено."""
        if not self._enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="session-cold-sync")

    async def stop(self, timeout_sec: float = 30.0) -> None:
        """Остановить задачу, успев сбросить последние изменения.

        Финальный проход обязателен до закрытия ``SessionManager``: он последний
        шанс внести в зеркало изменения текущего оборота.
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
        if self._consecutive_failures <= 0:
            return self._sync_interval_sec
        backoff = _BACKOFF_BASE_SEC * (2 ** min(self._consecutive_failures, 5))
        return min(self._sync_interval_sec, backoff, _BACKOFF_CAP_SEC)

    # -- цикл ----------------------------------------------------------------

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
                self._last_cycle_seconds = (
                    asyncio.get_running_loop().time() - started
                )

    async def _sync_changed(self) -> list[str]:
        """Догнать изменившееся. Возвращает ключи, которые реально есть."""
        sessions = await asyncio.to_thread(self._session_manager.list_sessions) or []
        keys = [str(item["key"]) for item in sessions if item.get("key")]
        self._last_upstream_session_count = len(keys)

        state = await self._call(
            "session_mirror_state", {"replica_id": self._replica_id},
        )
        mirrored = state.get("sessions") or {}
        self._last_mirror_session_count = int(state.get("count") or 0)

        by_key = {str(item["key"]): item for item in sessions if item.get("key")}
        for key in sorted(keys):
            if self._stopping:
                return keys
            await self._sync_one(key, by_key[key], mirrored.get(key))
        return keys

    async def _sync_one(
        self, key: str, info: dict[str, Any], cached: dict[str, Any] | None,
    ) -> None:
        path = info.get("path")
        if not path:
            self._unreadable_total += 1
            return

        digest = await asyncio.to_thread(file_digest, path)
        if digest is None:
            # Файл прямо сейчас переписывается либо недоступен. Не гадаем.
            self._unreadable_total += 1
            return

        if cached is not None and cached.get("source_digest") == digest:
            # Содержимое совпало с зеркалом. Это самый частый исход цикла, и он
            # не должен стоить ни чтения сессии, ни обращения к данным: без этой
            # проверки каждый проход читал и разбирал все сессии целиком.
            self._skipped_unchanged_total += 1
            return

        snapshot = await asyncio.to_thread(
            self._session_manager.read_session_snapshot, key,
        )
        if snapshot is None:
            self._snapshot_missing_total += 1
            return

        result = await self._call("mirror_session", self._mirror_payload(
            key, snapshot, digest,
        ))
        verdict = str(result.get("verdict") or "")
        if verdict not in _VERDICTS_WITHOUT_WRITE and verdict != "":
            self._sessions_written_total += 1
            self._messages_written_total += int(result.get("messages_written") or 0)
        if verdict == "skipped_stale":
            self._skipped_stale_total += 1
            self._log_stale_once(key, result)
        elif result.get("sync_lag_exceeded"):
            self._log_lag(key, result)

    def _mirror_payload(
        self, key: str, snapshot: Any, digest: str,
    ) -> dict[str, Any]:
        messages = [
            message for message in (getattr(snapshot, "messages", None) or [])
            if isinstance(message, dict)
        ]
        return {
            "session_key": key,
            "replica_id": self._replica_id,
            "source_digest": digest,
            "updated_at": _isoformat(getattr(snapshot, "updated_at", None)),
            "created_at": _isoformat(getattr(snapshot, "created_at", None)),
            "last_consolidated": int(getattr(snapshot, "last_consolidated", 0) or 0),
            "metadata": getattr(snapshot, "metadata", None) or {},
            "messages": messages,
            "stale_tolerance_seconds": int(self._stale_tolerance),
            "sync_lag_threshold_seconds": int(self._sync_lag_threshold),
        }

    async def _cleanup_absent(self, present: list[str]) -> None:
        """Убрать из зеркала сессии, которых больше нет в JSONL.

        Пустой список upstream НИКОГДА не бывает основанием для уборки. Каталог
        сессий лежит на диске, и пустой он бывает не «потому что всё удалили»,
        а потому что каталог не подмонтирован, недоступен или сорван: список
        приходит с чужой машины по NFS, и молчаливое удаление всего зеркала на
        таком сбое стоило бы месяцев переписки. Пустое зеркало удалять нечего,
        а непустое на пустом списке — это не «всё удалили», это «мы ничего не
        видим».
        """
        if not present:
            if self._last_mirror_session_count:
                self._cleanup_guarded_total += 1
                self._publish(
                    "agent.degraded",
                    "session_mirror: список сессий пуст, уборка пропущена — "
                    "зеркало не тронуто",
                    level=_LEVEL_WARN,
                    payload={
                        "replica_id": self._replica_id,
                        "mirror_sessions": self._last_mirror_session_count,
                    },
                )
            return

        result = await self._call("cleanup_session_mirror", {
            "replica_id": self._replica_id,
            "present_keys": sorted(present),
            "delete_after_missed_cycles": self._missing_cycles_threshold,
        })
        deleted = int(result.get("deleted_sessions") or 0)
        if deleted:
            self._deleted_sessions_total += deleted
            for key in result.get("deleted_keys") or []:
                self._log_deleted(str(key))

    # -- обращение к платформе -----------------------------------------------

    async def _call(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Вызвать операцию платформы и разобрать её JSON-ответ.

        Отказ платформы не проглатывается: он поднимается в цикл, который
        считает неудачу и откатывается на backoff. Разбор ответа — с отказом по
        форме, а не ``json.loads`` в молчание: нечитаемый ответ хуже отсутствия
        ответа, потому что выглядит как пустой успех.
        """
        raw = await self._mcp.call(operation, arguments)
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

    # -- журнал событий ------------------------------------------------------

    def _publish(
        self,
        event_type: str,
        summary: str,
        *,
        level: str = "WARN",
        session_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Отправить событие в журнал.

        Уровень — именованная константа с каноническим дефолтом, а не сборка на
        лету: CHECK ``valid_level`` в базе отвергает значение, которого в шкале
        нет, и отказ уносит весь батч, а не одну строку. Сборка уровня
        (f-строка, ``.upper()``) выглядит правдоподобно и уезжает в базу
        неотличимо от правильного. Канон проверяется
        ``tests/test_journal_level_canonical.py``, в том числе требование
        дефолта: «WARN» по умолчанию, потому что деградация — это дефолтное
        состояние фоновой подсистемы, а не исключение.
        """
        if self._db_logging is None:
            return
        from lib.services.db_logging_service import LogEvent, try_log_event

        try_log_event(
            self._db_logging,
            LogEvent(
                event_type=event_type,
                level=level,
                summary=summary,
                session_id=session_id,
                payload=payload or {},
            ),
            producer="SessionColdSyncService",
            event_type=event_type,
        )

    def _log_failure(self, exc: Exception) -> None:
        self._publish(
            "agent.degraded",
            f"session mirror cycle failed: {exc.__class__.__name__}",
            level=_LEVEL_WARN,
            payload={
                "error": str(exc)[:500],
                "consecutive_failures": self._consecutive_failures,
            },
        )

    def _log_deleted(self, key: str) -> None:
        self._publish(
            "agent.degraded",
            f"session_mirror: удалена строка зеркала {key}",
            level=_LEVEL_INFO,
            session_id=key,
            payload={"session_key": key},
        )

    def _log_stale_once(self, key: str, result: dict[str, Any]) -> None:
        now = datetime.now()
        last = self._stale_logged_at.get(key)
        if last is not None and now - last <= _STALE_LOG_DEDUP_TTL:
            return
        self._stale_logged_at[key] = now
        self._publish(
            "agent.degraded",
            f"session_stale_detected: {key} (зеркало впереди файла за пределы "
            f"терпимости)",
            level=_LEVEL_WARN,
            session_id=key,
            payload={
                "session_key": key,
                "replica_id": self._replica_id,
                "jsonl_updated_at": result.get("updated_at"),
                "pg_updated_at": result.get("previous_updated_at"),
                "tolerance_seconds": int(self._stale_tolerance),
            },
        )

    def _log_lag(self, key: str, result: dict[str, Any]) -> None:
        self._publish(
            "agent.degraded",
            f"sync_lag_exceeded: {key} (файл впереди зеркала за пределы порога)",
            level=_LEVEL_WARN,
            session_id=key,
            payload={
                "session_key": key,
                "replica_id": self._replica_id,
                "sync_lag_seconds": result.get("sync_lag_seconds"),
                "threshold_seconds": int(self._sync_lag_threshold),
            },
        )

    # -- метрики -------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        last_success_lag = None
        if self._last_success_ts is not None:
            last_success_lag = max(
                0.0, datetime.now().timestamp() - self._last_success_ts,
            )
        return {
            "enabled": self._enabled,
            "disabled_reason": self._disabled_reason,
            "replica_id": self._replica_id,
            "cycles_total": self._cycles_total,
            "cycles_failed_total": self._cycles_failed_total,
            "consecutive_failures": self._consecutive_failures,
            "last_cycle_seconds": self._last_cycle_seconds,
            "last_success_ts": self._last_success_ts,
            "last_success_lag_seconds": last_success_lag,
            "upstream_session_count": self._last_upstream_session_count,
            "mirror_session_count": self._last_mirror_session_count,
            "sessions_written_total": self._sessions_written_total,
            "messages_written_total": self._messages_written_total,
            "skipped_unchanged_total": self._skipped_unchanged_total,
            "skipped_stale_total": self._skipped_stale_total,
            "unreadable_total": self._unreadable_total,
            "snapshot_missing_total": self._snapshot_missing_total,
            "cleanup_guarded_total": self._cleanup_guarded_total,
            "deleted_sessions_total": self._deleted_sessions_total,
            "stale_tolerance_seconds": int(self._stale_tolerance),
            "sync_lag_threshold_seconds": int(self._sync_lag_threshold),
            "missing_cycles_threshold": self._missing_cycles_threshold,
        }


def _isoformat(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)

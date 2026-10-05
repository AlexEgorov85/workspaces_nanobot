"""Зеркалирование сессий в холодное хранилище через платформу.

Первый наследник ``MirrorPoller``: здесь только то, что знать про JSONL-сессии.
Механизм (цикл, дайджест, уборка, отказы, журнал, метрики) живёт в
``lib/gateway/mirror/mirror_poller.py``.

Источник истины — upstream ``SessionManager`` (JSONL). Холодное зеркало в
PostgreSQL обслуживается только для multi-instance, наблюдаемости и аварийного
восстановления; пишет его эта подсистема, и пишет она через операции платформы
``mirror_session`` / ``cleanup_session_mirror`` / ``session_mirror_state``, а не
напрямую.

Почему через платформу, а не пул агента
--------------------------------------
Тот же путь, по которому ушли канал и журнал: имя таблицы зеркала тогда было
объявлено и в конфигурации агента, и в ``platform.json``, и агент видел только
второй источник. Оверлей профиля применялся на платформе, а писатель смотрел
в другую сторону — и писал мимо. Здесь такой возможности нет вовсе: имена
таблиц живут только на платформе.

Почему решение о записи принимает платформа
------------------------------------------
Сравнение «зеркало против источника» и сама запись должны быть в одной
транзакции. Пока это были разные вызовы, между ними успевал вклиниться второй
писатель, а разрыв метаданных и сообщений после сбоя оставлял зеркало
разорванным навсегда: признак «изменилось» у сессии уже совпадал бы с
записанным, и следующие циклы проходили мимо. Операция ``mirror_session``
делает чтение, решение и запись одной транзакцией, а запись — условным
``UPDATE`` (на Greenplum 6.5 ``SELECT ... FOR UPDATE`` взял бы блокировку
уровня таблицы).

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

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from lib.gateway.mirror.mirror_poller import (
    MirrorEntry,
    MirrorPoller,
    _LEVEL_WARN,
    _STALE_LOG_DEDUP_TTL,
)

if TYPE_CHECKING:
    from nanobot.session.manager import SessionManager

    from lib.services.db_logging_service import DbLoggingService

#: Операции платформы, которыми ведётся запись. Объявлены именами, а не
#: разбросаны литералами по вызовам: сверка ``MIRROR_OPERATIONS`` с тем, что
#: платформа действительно регистрирует, ловит случай, когда операция описана
#: в коде, но отсутствует в реестре — вызов проходит чтение кода, компиляцию и
#: все тесты с подставным клиентом, а падает в рантайме на каждом цикле.
#: Проверка: ``tests/test_session_mirror_wire.py``.
OP_MIRROR = "data.mirror_session"
OP_CLEANUP = "data.cleanup_session_mirror"
OP_STATE = "data.session_mirror_state"
MIRROR_OPERATIONS = (OP_MIRROR, OP_CLEANUP, OP_STATE)


def file_digest(path: Any) -> str | None:
    """Дайджест файла-сессии, либо ``None``, если он сейчас непригоден.

    Отказ по форме, а не исключение: файл переписывается на лету, и «прочитать
    не вышло» — штатный исход, который обязан дождаться следующего цикла, а не
    обрывать его.

    Файл читается целиком и сверяется тремя признаками до и после чтения
    (размер, ``mtime``, совпадение длины с ``stat`` после). Без сверки
    ``JsonlSessionStore``, пишущий файл перезаписью, подсовывает дайджест
    половины файла, и зеркало записало бы оборванную сессию как целую.
    """
    if not path:
        return None
    file_path = Path(path)
    try:
        before = file_path.stat()
        payload = file_path.read_bytes()
        after = file_path.stat()
    except (OSError, TypeError, ValueError):
        return None
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(payload) != after.st_size
    ):
        return None
    return hashlib.sha256(payload).hexdigest()


def _isoformat(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class SessionMirror(MirrorPoller):
    """Зеркало сессий JSONL → PostgreSQL.

    Наследник объявляет ресурс и шаги работы с ним; решение о записи, уборку,
    отказы, журнал и метрики наследует.

    Аргументы конструктора — все с дефолтами. DSN и имена таблиц сюда НЕ
    приходят: и то, и другое принадлежит платформе, и второй экземпляр
    объявления разошёлся бы с первым при первой же смене настройки.
    """

    resource_name: ClassVar[str] = "session_mirror"
    state_operation: ClassVar[str] = OP_STATE
    write_operation: ClassVar[str] = OP_MIRROR
    cleanup_operation: ClassVar[str] = OP_CLEANUP

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
        super().__init__(
            enterprise_mcp=enterprise_mcp,
            replica_id=replica_id,
            enabled=enabled,
            sync_interval_sec=sync_interval_sec,
            missing_cycles_threshold=missing_cycles_threshold,
            db_logging_service=db_logging_service,
            task_name="session-mirror",
        )
        self._session_manager = session_manager
        self._stale_tolerance = max(0, int(stale_tolerance_seconds))
        self._sync_lag_threshold = max(0, int(sync_lag_threshold_seconds))
        self._messages_written_total = 0
        self._skipped_stale_total = 0
        self._stale_logged_at: dict[str, datetime] = {}

        if sync_lag_threshold_seconds < stale_tolerance_seconds:
            raise ValueError(
                f"sync_lag_threshold_seconds ({sync_lag_threshold_seconds}) "
                f"must be >= stale_tolerance_seconds ({stale_tolerance_seconds})"
            )

    # -- шаги ресурса ----------------------------------------------------------

    def list_entries(self) -> list[MirrorEntry]:
        """Сессии, которые сейчас есть на диске.

        Список пуст не значит «всё удалили»: каталог лежит на диске, и пуст он
        бывает не потому, что данные исчезли, а потому что он не подмонтирован
        или сорван. Решение по пустому списку принимает механизм
        (``guard_empty_source``), и здесь важно лишь не превратить сбой каталога
        в «удалить всё».
        """
        sessions = self._session_manager.list_sessions() or []
        return [
            MirrorEntry(key=str(item["key"]), locator=item.get("path"))
            for item in sessions
            if item.get("key")
        ]

    def digest_of(self, entry: MirrorEntry) -> str | None:
        if not entry.locator:
            return None
        return file_digest(entry.locator)

    def read_source(self, key: str) -> Any:
        return self._session_manager.read_session_snapshot(key)

    def build_write_arguments(
        self, key: str, source: Any, digest: str
    ) -> dict[str, Any]:
        messages = [
            message for message in (getattr(source, "messages", None) or [])
            if isinstance(message, dict)
        ]
        return {
            "session_key": key,
            "replica_id": self.replica_id,
            "source_digest": digest,
            "updated_at": _isoformat(getattr(source, "updated_at", None)),
            "created_at": _isoformat(getattr(source, "created_at", None)),
            "last_consolidated": int(getattr(source, "last_consolidated", 0) or 0),
            "metadata": getattr(source, "metadata", None) or {},
            "messages": messages,
            "stale_tolerance_seconds": int(self._stale_tolerance),
            "sync_lag_threshold_seconds": int(self._sync_lag_threshold),
        }

    def after_write(self, key: str, result: dict[str, Any]) -> None:
        """Счётчики и журнал по вердикту платформы."""
        verdict = str(result.get("verdict") or "")
        if verdict not in ("", "unchanged", "skipped_equal",
                           "skipped_within_tolerance", "skipped_stale"):
            self._messages_written_total += int(result.get("messages_written") or 0)
        if verdict == "skipped_stale":
            self._skipped_stale_total += 1
            self._log_stale_once(key, result)
        elif result.get("sync_lag_exceeded"):
            self._log_lag(key, result)

    def extra_stats(self) -> dict[str, Any]:
        return {
            "messages_written_total": self._messages_written_total,
            "skipped_stale_total": self._skipped_stale_total,
            "stale_tolerance_seconds": int(self._stale_tolerance),
            "sync_lag_threshold_seconds": int(self._sync_lag_threshold),
            "upstream_session_count": self._last_source_count,
            "mirror_session_count": self._last_mirror_count,
            "sessions_written_total": self._written_total,
            "snapshot_missing_total": self._source_missing_total,
            "cleanup_guarded_total": self._cleanup_guarded_total,
            "deleted_sessions_total": self._deleted_total,
        }

    # -- журнал: сессионные события -------------------------------------------

    def _log_stale_once(self, key: str, result: dict[str, Any]) -> None:
        """Отметить разошедшееся зеркало, не повторяясь на каждом цикле.

        Событие одно и то же из цикла в цикл, пока сессию не починят; без
        дедупликации журнал забивается одинаковыми строками и перестаёт быть
        читаемым ровно тогда, когда читать есть что.
        """
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
                "replica_id": self.replica_id,
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
                "replica_id": self.replica_id,
                "sync_lag_seconds": result.get("sync_lag_seconds"),
                "threshold_seconds": int(self._sync_lag_threshold),
            },
        )

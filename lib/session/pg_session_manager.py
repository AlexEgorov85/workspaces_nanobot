"""Compatibility layer: PGSessionManager теперь — cold-storage mirror.

После ``storage-hybridization`` (см. спеку
``openspec/specs/storage/session-hybridization/spec.md``) этот
класс **НЕ пишет** в ``agent_session_meta`` / ``agent_session_messages``
напрямую. Hot-path операции (``get_or_create``, ``save``,
``list_sessions``, ``read_session_metadata``, ``read_session_file``)
делегируются в upstream ``SessionManager`` (JSONL), который является
единственным source of truth.

PG остаётся как cold-storage mirror через отдельный фоновый сервис
``SessionColdSyncService``. См.:

- ``lib/services/session_cold_sync_service.py`` — зеркалирование;
- ``docs/architecture/storage-layers.md`` — общая модель хранения;
- design ``openspec/changes/storage-hybridization/design.md`` § D6
  и § D-Pool.

Этот класс сохранён исключительно как тонкий compatibility layer для
56 call-sites, использующих ``PGSessionManager``-импорт и его
конструкторские параметры (``dsn``, ``schema``, ``messages_table``,
``meta_table``). Никаких side-effect'ов в hot path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.session.manager import Session, SessionManager


class PGSessionManager(SessionManager):
    """Cold-storage mirror поверх upstream ``SessionManager``.

    Hot-path методы (``get_or_create``, ``save``, ``list_sessions``,
    ``read_session_metadata``, ``read_session_file``) делегируются в
    ``super()`` (upstream JSONL). Никаких прямых SQL-операций в hot
    path — это архитектурный инвариант (см. design D6 / R6,
    ``tests/test_storage_hybridization.py::test_no_direct_sql_to_session_tables_in_hot_path``).

    Конструкторские параметры (``dsn``, ``schema``, ``messages_table``,
    ``meta_table``) сохранены для обратной совместимости с 56 call-sites;
    ``SessionColdSyncService`` получает DSN / имена таблиц из
    ``ApplicationContext`` отдельно (не из этого класса).
    """

    def __init__(
        self,
        workspace: Path,
        dsn: str = "",
        schema: str = "public",
        messages_table: str = "",
        meta_table: str = "",
        **kwargs: Any,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        if not messages_table or not meta_table:
            raise ValueError(
                "PGSessionManager: messages_table и meta_table обязательны "
                "(channels.postgres.messages_table / meta_table). "
                f"messages_table={messages_table!r}, meta_table={meta_table!r}"
            )
        super().__init__(workspace=self.workspace)
        self._schema = schema
        self._meta_table = meta_table
        self._messages_table = messages_table
        self._fq_meta = self._quote(f"{schema}.{meta_table}")
        self._fq_messages = self._quote(f"{schema}.{messages_table}")

        if dsn:
            from utils.db import configure as _cfg
            _cfg(dsn)

    def close(self) -> None:
        """No-op: PG-соединения живут в общем пуле ``utils.db``."""
        return None

    def get_or_create(self, key: str) -> Session:
        """Делегирует в upstream ``SessionManager`` (JSONL).

        Раньше этот метод читал/писал ``agent_session_meta`` /
        ``agent_session_messages`` напрямую. Теперь — единственный
        writer сессий upstream (см. design D6).
        """
        return super().get_or_create(key)

    def save(self, session: Session, *, fsync: bool = False) -> None:
        """Делегирует в upstream ``SessionManager.save`` (JSONL).

        Никаких прямых ``INSERT/UPDATE`` в
        ``agent_session_meta`` / ``agent_session_messages`` — это
        архитектурный инвариант (см. test_storage_hybridization).
        """
        super().save(session, fsync=fsync)

    def list_sessions(self) -> list[dict[str, Any]]:
        """Делегирует в upstream ``SessionManager.list_sessions``."""
        return super().list_sessions()

    def read_session_metadata(self, key: str) -> dict[str, Any] | None:
        """Делегирует в upstream ``SessionManager.read_session_metadata``."""
        return super().read_session_metadata(key)

    def read_session_file(self, key: str) -> dict[str, Any] | None:
        """Делегирует в upstream ``SessionManager.read_session_file``."""
        return super().read_session_file(key)

    def invalidate(self, key: str) -> None:
        """No-op: ``SessionManager`` (upstream) сам управляет кешем."""
        return None

    def delete_session(self, key: str) -> bool:
        """Делегирует в upstream ``SessionManager.delete_session``."""
        return super().delete_session(key)

    def flush_all(self) -> int:
        """No-op: upstream ``SessionManager`` сам флашит JSONL при shutdown.

        Возвращает 0 (нет кеша для flush'а — кеш живёт в upstream).
        """
        return 0

    @staticmethod
    def _validate_ident(part: str) -> None:
        if not part or not part.replace("_", "").replace("$", "").isalnum():
            raise ValueError(f"Unsafe SQL identifier part: {part!r}")

    @classmethod
    def _quote(cls, ident: str) -> str:
        parts = ident.split(".")
        for part in parts:
            cls._validate_ident(part)
        return ".".join(f'"{p}"' for p in parts)

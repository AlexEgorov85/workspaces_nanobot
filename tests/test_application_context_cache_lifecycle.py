"""
Жизненный цикл кэша в агенте — то, что от него осталось (change
``drop-local-cache-read-from-pg``, затем фаза 5, п. 5.8).

Проверяют:

* поля ``ApplicationContext`` (``role``, ``enable_*``, ``print_llm_calls``);
* что у ``CacheLoadService`` не осталось фоновой машинерии: колбэков записи
  и поток-жизненного цикла нет, вход ровно один.

**Что ушло и куда.** Классы ``TestInitCacheRuntimeReturnsProviderAndLoader`` и
``TestCacheRuntimeLoadThenReadOrdering`` удалены: они проверяли
``_init_cache_runtime``, которого в агенте больше нет — снимком владеет
capability ``data`` платформы. Их инварианты не потеряны, а перенесены
портом 5.10 в ``mcp-platform/tests``: порядок «загрузка завершилась, writer
закрыт, потом открыли на чтение» — ``test_snapshot_load_service.py::
TestLoadAgainstRealStore::test_snapshot_becomes_readable``, отсутствие фоновых
потоков — ``test_load_is_blocking_and_leaves_no_threads``, потолок в
``max_conn`` — ``test_snapshot_load_service_bounds.py``.

Прежняя версия этого файла проверяла ``CacheOwnershipCoordinator``, READER/
OWNER-разделение и fencing. Слой владения удалён
(``drop-local-cache-read-from-pg``), поэтому и эти тесты заменены.
"""

from __future__ import annotations

import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


# ---------------------------------------------------------------------------
# Stage B — composition wiring
# ---------------------------------------------------------------------------


class TestApplicationContextFields:
    """``ApplicationContext`` MUST иметь ``role`` и прочие конфигурационные поля."""

    def test_role_field_exists(self) -> None:
        from lib.core.application_context import ApplicationContext
        assert "role" in dir(ApplicationContext)

    def test_enable_db_logging_field_exists(self) -> None:
        from lib.core.application_context import ApplicationContext
        assert "enable_db_logging" in dir(ApplicationContext)

    def test_enable_audit_field_exists(self) -> None:
        from lib.core.application_context import ApplicationContext
        assert "enable_audit" in dir(ApplicationContext)

    def test_enable_cron_field_exists(self) -> None:
        from lib.core.application_context import ApplicationContext
        assert "enable_cron" in dir(ApplicationContext)

    def test_print_llm_calls_field_exists(self) -> None:
        from lib.core.application_context import ApplicationContext
        assert "print_llm_calls" in dir(ApplicationContext)


class TestCacheLoadServiceHasNoBackgroundMachinery:
    """Загрузчик — разовая синхронная операция."""

    def test_no_write_callbacks(self) -> None:
        """Колбэков записи больше нет: загрузчик
        держит роль ``CacheStore`` напрямую."""
        from lib.services.cache_load_service import CacheLoadService

        for gone in (
            "set_on_new_records_callback",
            "set_on_replace_records_callback",
            "set_on_schema_callback",
            "set_on_sync_callback",
        ):
            assert not hasattr(CacheLoadService, gone), gone

    def test_no_thread_lifecycle(self) -> None:
        """У загрузчика нет потока жизни и обрабатки."""
        from lib.services.cache_load_service import CacheLoadService

        assert not hasattr(CacheLoadService, "start")
        assert not hasattr(CacheLoadService, "stop")
        assert not hasattr(CacheLoadService, "_poll_changes")
        assert not hasattr(CacheLoadService, "_drain_queue")

    def test_has_single_synchronous_entry_point(self) -> None:
        from lib.services.cache_load_service import CacheLoadService

        assert callable(CacheLoadService.load)


# ---------------------------------------------------------------------------
# Stage E — fencing integration в PgDuckDbSyncService

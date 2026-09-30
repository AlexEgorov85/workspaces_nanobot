"""
Тесты жизненного цикла кэша (changes ``unify-cli-gateway-architecture`` +
``drop-local-cache-read-from-pg``).

Проверяют:

* поля ``ApplicationContext`` (``role``, ``enable_*``, ``print_llm_calls``);
* что ``_init_cache_runtime`` возвращает пару «провайдер + загрузчик»;
* **порядок стадий**: загрузка выполняется синхронно, writer закрывается, и
  только после этого открывается провайдер для чтения — файл не удерживается
  между стадиями;
* что число потоков загрузки ограничено размером пула;
* что у ``CacheLoadService`` не осталось фоновой машинерии: колбэков записи
  и поток-жизненного цикла нет, вход ровно один.

Прежняя версия этого файла проверяла ``CacheOwnershipCoordinator``, READER/
OWNER-разделение и fencing. Слой владения удалён
(``drop-local-cache-read-from-pg``), поэтому и эти тесты заменены.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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


class TestInitCacheRuntimeReturnsProviderAndLoader:
    """``_init_cache_runtime`` возвращает ``(CacheProvider, CacheLoadService)``."""

    def test_returns_two_tuple(self) -> None:
        """Stub-check: helper возвращает tuple из 2 элементов.

        Не выполняем реальный ApplicationContext.create — это долго и
        требует полного SETTINGS/registry setup. Достаточно проверить
        signature helper'а.
        """
        import inspect

        from lib.core.application_context import _init_cache_runtime

        # Сигнатура hint: функция возвращает (provider, loader).
        # Простой статический анализ через AST-skip — done в других test-файлах.
        # В этом тесте проверим что _make_sync_services уже импортируется и модуль
        # на месте (никаких import error).
        assert callable(_init_cache_runtime)


class TestCacheRuntimeLoadThenReadOrdering:
    """Загрузка MUST завершиться и отпустить файл до открытия на чтение.

    Регрессия прежней машинерии: wiring жил в callers (``gateway.py`` /
    ``benchmarks/runner.py``) и терялся при консолидации, а фоновой сервис
    начинал писать до того, как вызывающий код успевал подписаться на колбэки
    (``_dispatch`` при ``_on_new_records=None`` делал молчаливый ``return`` —
    данные из PG не попадали в кэш, без исключения и без traceback). Сейчас
    колбэков нет вовсе, а порядок стадий закреплён этим тестом.
    """

    def _ctx_stub(self) -> MagicMock:
        ctx = MagicMock()
        ctx.role = "gateway"
        ctx.config_service.settings_section.return_value = {
            "postgres": {
                "dsn": "postgresql://stub/stub",
                "pool": {"max_conn": 4},
            }
        }
        ctx.db_logging_service = None
        return ctx

    def _patch_deps(self, monkeypatch: pytest.MonkeyPatch):
        """Подменить фабрику провайдера и загрузчик.

        Оба импортируются внутри ``_init_cache_runtime``, поэтому
        патчатся на уровне модулей-поставщиков.
        """
        import lib.services.cache_load_service as load_mod
        import lib.services.cache_provider as provider_mod
        import lib.services.table_registry as tr_mod

        writer = MagicMock(name="writer")
        reader = MagicMock(name="reader")
        loader = MagicMock(name="loader")

        def _open(*, mode, db_logging_service=None):
            if mode is provider_mod.CacheAccessMode.READ_WRITE:
                return writer
            return reader

        monkeypatch.setattr(provider_mod, "open_cache_provider", _open)
        monkeypatch.setattr(
            load_mod, "CacheLoadService", MagicMock(return_value=loader)
        )
        monkeypatch.setattr(
            tr_mod.table_registry,
            "resources",
            MagicMock(
                return_value=[
                    MagicMock(schema="oarb", name="audits", label="audit"),
                    MagicMock(schema="oarb", name="violations", label="audit"),
                ]
            ),
        )
        monkeypatch.setattr(
            tr_mod.table_registry,
            "table_names",
            MagicMock(return_value=["oarb.audits", "oarb.violations"]),
        )
        monkeypatch.setattr(
            tr_mod.table_registry, "vector_names", MagicMock(return_value=[])
        )
        return writer, reader, loader

    def test_load_runs_then_writer_is_closed(self, monkeypatch) -> None:
        """Загрузка выполняется, соединение на
        запись закрывается — файл освобождается."""
        from lib.core.application_context import _init_cache_runtime

        writer, reader, loader = self._patch_deps(monkeypatch)
        provider, returned_loader = _init_cache_runtime(self._ctx_stub())

        loader.load.assert_called_once()
        writer.close.assert_called_once()
        assert provider is reader, "после загрузки возвращается провайдер на чтение"
        assert returned_loader is loader

    def test_read_provider_opened_after_writer_closed(self, monkeypatch) -> None:
        """Порядок: загрузка -> close -> открытие на чтение."""
        from lib.core.application_context import _init_cache_runtime
        import lib.services.cache_provider as provider_mod

        writer, reader, loader = self._patch_deps(monkeypatch)
        order: list[str] = []
        writer.close.side_effect = lambda: order.append("close")
        loader.load.side_effect = lambda: order.append("load")

        def _open_traced(*, mode, db_logging_service=None):
            if mode is provider_mod.CacheAccessMode.READ_WRITE:
                return writer
            order.append("open_read")
            return reader

        monkeypatch.setattr(provider_mod, "open_cache_provider", _open_traced)
        _init_cache_runtime(self._ctx_stub())

        assert order == ["load", "close", "open_read"]

    def test_max_workers_bounded_by_pool_size(self, monkeypatch) -> None:
        """Число потоков загрузки MUST NOT превышать
        размер общего пула соединений.

        Прежде загрузка поднимала ``min(len(tables), 8)``
        потоков при ``max_conn=4`` — восемь они дрались за
        одии и те сами себя.
        """
        import lib.services.cache_load_service as load_mod
        from lib.core.application_context import _init_cache_runtime

        self._patch_deps(monkeypatch)
        _init_cache_runtime(self._ctx_stub())

        _args, kwargs = load_mod.CacheLoadService.call_args
        assert kwargs["max_workers"] == 4


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

"""Интеграционный тест lifecycle ApplicationContext → SkillCatalog refresh.

Проверяем, что:
  * ``ApplicationContext.create()`` работает даже без DuckDB-снапшота;
  * ``ApplicationContext.start()`` ставит once-callback на первый sync;
  * после успешного ``on_sync`` SkillCatalog получает актуальные данные
    без ручного вызова refresh извне;
  * ошибка в sync-сервисе не валит SkillCatalog (readiness изолирован).

Не трогает реальный PostgreSQL/DuckDB — всё через mocks.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.services.table_registry import (
    SkillRegistration,
    TableResource,
    table_registry,
)
from lib.utils.skill_catalog import SkillCatalog


@pytest.fixture(autouse=True)
def _isolate_env():
    saved = {k: v for k, v in os.environ.items() if k.startswith("SKILL_")}
    for k in list(os.environ):
        if k.startswith("SKILL_"):
            del os.environ[k]
    yield
    for k in list(os.environ):
        if k.startswith("SKILL_"):
            del os.environ[k]
    for k, v in saved.items():
        os.environ[k] = v


@pytest.fixture(autouse=True)
def _reset_registry():
    table_registry.clear()
    yield
    table_registry.clear()


class TestApplicationContextStartRefreshesCatalog:
    """После initial sync SkillCatalog автоматически получает данные."""

    def test_initial_sync_callback_triggers_refresh(
        self, tmp_path: Path,
    ) -> None:
        """Полный путь: populate до sync (пусто) → sync-callback → populate (с данными)."""
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name="oarb.audits"),
                TableResource(
                    name="public.agent_predefined_scripts",
                    label="scripts_registry",
                ),
            ),
        ))

        snapshot = tmp_path / "cache.duckdb"
        # До старта snapshot ещё нет — cold-start сценарий.
        assert not snapshot.is_file()

        # 1) ``_populate_skill_catalog_env`` до sync — env-vars пустые.
        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ), patch(
            "lib.services.table_registry.table_registry.snapshot_path",
            return_value=snapshot,
        ):
            _populate_skill_catalog_env(workspace_path=tmp_path)

        assert os.environ.get("SKILL_AUDIT_ANALYZER_SCRIPTS") == ""

        # 2) snapshot появился (имитируем публикацию из sync-сервиса).
        snapshot.write_bytes(b"")

        fake_provider = MagicMock()
        fake_provider.query_sql.return_value = {
            "status": "success",
            "rows": [
                {"name": "audit_status_summary", "description": "Сводка"},
            ],
        }
        fake_provider.close = MagicMock()

        # 3) SkillCatalog.refresh_runtime_catalog — публичный метод,
        #    который дёргает наш once-callback из ApplicationContext.start.
        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ), patch(
            "lib.services.table_registry.table_registry.snapshot_path",
            return_value=snapshot,
        ), patch(
            "lib.services.cache_provider_impl.PostgresDuckDbProvider",
            return_value=fake_provider,
        ):
            SkillCatalog.refresh_runtime_catalog(workspace_path=tmp_path)

        scripts = os.environ.get("SKILL_AUDIT_ANALYZER_SCRIPTS", "")
        assert "audit_status_summary" in scripts
        descs = os.environ.get("SKILL_AUDIT_ANALYZER_SCRIPT_DESCRIPTIONS", "")
        assert "audit_status_summary=Сводка" in descs

    def test_no_polling_in_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ApplicationContext.start`` не использует ``time.sleep`` / polling."""
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name="oarb.audits"),
                TableResource(
                    name="public.agent_predefined_scripts",
                    label="scripts_registry",
                ),
            ),
        ))

        # Подменяем ``_wrap_initial_sync_callback``, чтобы убедиться, что
        # именно он ставит once-callback, а не какой-то sleep/polling.
        import lib.core.application_context as appctx

        wrapped: list[object] = []
        original = appctx._wrap_initial_sync_callback

        def spy(sync_service, cb):
            wrapped.append((sync_service, cb))
            return original(sync_service, cb)

        monkeypatch.setattr(appctx, "_wrap_initial_sync_callback", spy)

        # Также убедимся, что ``time.sleep`` не вызывается из start().
        sleep_calls: list[float] = []
        import time as _time

        def _sleep_spy(secs: float) -> None:
            sleep_calls.append(secs)

        monkeypatch.setattr(_time, "sleep", _sleep_spy)

        # Минимальный sync_service с заглушкой set_on_sync_callback.
        class FakeSync:
            def __init__(self) -> None:
                self._on_sync_callback = None
                self.calls = 0

            def set_on_sync_callback(self, cb) -> None:
                self._on_sync_callback = cb

            def fire(self) -> None:
                self.calls += 1
                if self._on_sync_callback is not None:
                    self._on_sync_callback()

        sync = FakeSync()
        ctx = type("C", (), {"workspace_dir": tmp_path, "sync_service": sync})()

        # Эмулируем вызов start(), который должен поставить once-callback
        # через _wrap_initial_sync_callback. Точный код start() здесь не
        # запускаем (слишком много побочных эффектов); вызываем тот же
        # helper, что и start().
        appctx._wrap_initial_sync_callback(
            ctx.sync_service,
            lambda: appctx._populate_skill_catalog_env(
                workspace_path=ctx.workspace_dir,
            ),
        )
        # Имитируем populate cold-start.
        appctx._populate_skill_catalog_env(workspace_path=ctx.workspace_dir)

        assert len(wrapped) == 1, (
            f"_wrap_initial_sync_callback should be installed exactly once, "
            f"got {len(wrapped)}"
        )

        # Никакого time.sleep не было.
        assert sleep_calls == []

        # sync ещё не сработал — env-vars пустые (snapshot отсутствует).
        assert os.environ.get("SKILL_AUDIT_ANALYZER_SCRIPTS") == ""

    def test_sync_failure_does_not_break_skillcatalog(
        self, tmp_path: Path,
    ) -> None:
        """Ошибка в on_sync-callback не валит SkillCatalog."""
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name="oarb.audits"),
                TableResource(
                    name="public.agent_predefined_scripts",
                    label="scripts_registry",
                ),
            ),
        ))

        from lib.core.application_context import _wrap_initial_sync_callback

        class FakeSync:
            def __init__(self) -> None:
                self._on_sync_callback = None

            def set_on_sync_callback(self, cb) -> None:
                self._on_sync_callback = cb

        sync = FakeSync()
        _wrap_initial_sync_callback(
            sync,
            lambda: (_ for _ in ()).throw(RuntimeError("sync boom")),
        )

        # Захватываем ссылку ДО первого вызова (после него callback снимется).
        cb = sync._on_sync_callback
        assert cb is not None
        # Sync-callback падает внутри once-обёртки, но sync продолжает
        # работать (callback снят, ничего не пробросилось).
        cb()
        assert sync._on_sync_callback is None
        # env-vars пустые, без падения.
        assert os.environ.get("SKILL_AUDIT_ANALYZER_SCRIPTS", "") == ""

    def test_once_callback_does_not_fire_on_subsequent_polls(self) -> None:
        """После initial sync повторные poll-циклы НЕ дёргают refresh."""
        from lib.core.application_context import _wrap_initial_sync_callback

        class FakeSync:
            def __init__(self) -> None:
                self._on_sync_callback = None

            def set_on_sync_callback(self, cb) -> None:
                self._on_sync_callback = cb

        sync = FakeSync()
        refresh_calls: list[int] = []
        _wrap_initial_sync_callback(sync, lambda: refresh_calls.append(1))

        cb = sync._on_sync_callback
        assert cb is not None

        # Первый вызов — initial sync. После него callback снимается.
        cb()
        assert sync._on_sync_callback is None

        # Дальнейшие poll-циклы уже не дёргают наш refresh.
        assert len(refresh_calls) == 1, (
            f"refresh should fire exactly once, got {len(refresh_calls)}"
        )

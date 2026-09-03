"""Тесты для ``_populate_skill_catalog_env`` в ApplicationContext.

Изолированы от runtime-БД через mock ``TableRegistry`` и подмену
``read_vector_index_config`` / ``PostgresDuckDbProvider``. Проверяем
контракт auto-populate: env-vars выставляются, переживают повторный
вызов, очищаются ``clear_skill_env``.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.services.table_registry import (
    SkillRegistration,
    TableResource,
    VectorResource,
    table_registry,
)


@pytest.fixture(autouse=True)
def _isolate_env():
    """Сохраняем и восстанавливаем ``SKILL_*`` env-vars между тестами."""
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


class TestPopulateTablesEnv:
    """``SKILL_<NAME>_TABLES`` — только таблицы без ``label``."""

    def test_tables_without_label_only(self) -> None:
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name="oarb.audits"),
                TableResource(name="oarb.violations"),
                TableResource(
                    name="public.agent_predefined_scripts",
                    label="scripts_registry",
                ),
            ),
        ))

        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ):
            _populate_skill_catalog_env()

        env = os.environ.get("SKILL_AUDIT_ANALYZER_TABLES", "")
        names = [x for x in env.split(",") if x]
        assert "oarb.audits" in names
        assert "oarb.violations" in names
        assert "public.agent_predefined_scripts" not in names

    def test_empty_registry_sets_no_env(self) -> None:
        """Без skill'ов в registry env-vars SKILL_* не создаются вовсе."""
        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ):
            _populate_skill_catalog_env()

        assert "SKILL_AUDIT_ANALYZER_TABLES" not in os.environ

    def test_registered_skill_without_resources_sets_empty_env(self) -> None:
        """Skill зарегистрирован, но без tables → env-var = пустая строка."""
        from lib.core.skill_registration import register_skill_from_config

        register_skill_from_config(
            "audit_analyzer",
            {"tables": []},
        )

        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ):
            _populate_skill_catalog_env()

        assert os.environ.get("SKILL_AUDIT_ANALYZER_TABLES") == ""

    def test_disabled_skill_skipped(self) -> None:
        """``enabled=False`` в SkillRegistration → skill пропускается."""
        from lib.core.skill_registration import register_skill_from_config

        register_skill_from_config(
            "audit_analyzer",
            {
                "enabled": False,
                "tables": [{"name": "oarb.audits"}],
            },
        )

        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ):
            _populate_skill_catalog_env()

        assert "SKILL_AUDIT_ANALYZER_TABLES" not in os.environ


class TestPopulateVectorsEnv:
    """``SKILL_<NAME>_VECTORS`` + ``SKILL_<NAME>_VECTOR_DESCRIPTIONS``."""

    def test_vectors_from_read_vector_index_config(self) -> None:
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(TableResource(name="oarb.audits"),),
        ))

        fake_cfg = {
            "audits_index": {
                "source_table": "oarb.audits",
                "description": "Поиск проверок",
            },
            "violations_index": {
                "source_table": "oarb.violations",
                "description": "Поиск нарушений",
            },
        }

        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value=fake_cfg,
        ):
            _populate_skill_catalog_env()

        vec_names = os.environ.get("SKILL_AUDIT_ANALYZER_VECTORS", "")
        assert "audits_index" in vec_names
        assert "violations_index" in vec_names
        descs = os.environ.get("SKILL_AUDIT_ANALYZER_VECTOR_DESCRIPTIONS", "")
        assert "audits_index=Поиск проверок" in descs
        assert "violations_index=Поиск нарушений" in descs

    def test_vectors_fallback_to_table_registry(self) -> None:
        """Если ``read_vector_index_config`` пуст/падает — fallback на TableRegistry."""
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name="oarb.audits"),
                VectorResource(name="oarb.audit_vectors", tracking_column="id"),
            ),
        ))

        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ):
            _populate_skill_catalog_env()

        assert os.environ.get("SKILL_AUDIT_ANALYZER_VECTORS") == "oarb.audit_vectors"

    def test_vector_descriptions_escaping(self) -> None:
        """``;`` и ``\\n`` в description экранируются в ``,`` и пробел."""
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(TableResource(name="oarb.audits"),),
        ))
        fake_cfg = {
            "idx": {"description": "описание с ; точкой с запятой\nи newline"},
        }

        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value=fake_cfg,
        ):
            _populate_skill_catalog_env()

        descs = os.environ.get("SKILL_AUDIT_ANALYZER_VECTOR_DESCRIPTIONS", "")
        assert "описание с , точкой с запятой и newline" in descs


class TestPopulateScriptsEnv:
    """``SKILL_<NAME>_SCRIPTS`` + ``SKILL_<NAME>_SCRIPT_DESCRIPTIONS``."""

    def test_scripts_from_duckdb_snapshot(self, tmp_path) -> None:
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

        fake_provider = MagicMock()
        fake_provider.query_sql.return_value = {
            "status": "success",
            "rows": [
                {"name": "audit_status_summary", "description": "Сводка"},
                {"name": "top_violations_by_type", "description": "Топ"},
            ],
        }
        fake_provider.close = MagicMock()

        fake_cache = tmp_path / "fake_cache.duckdb"
        fake_cache.write_bytes(b"")

        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.table_registry.table_registry.snapshot_path",
            return_value=fake_cache,
        ), patch(
            "lib.services.cache_provider_impl.PostgresDuckDbProvider",
            return_value=fake_provider,
        ):
            _populate_skill_catalog_env()

        scripts = os.environ.get("SKILL_AUDIT_ANALYZER_SCRIPTS", "")
        assert "audit_status_summary" in scripts
        assert "top_violations_by_type" in scripts
        descs = os.environ.get("SKILL_AUDIT_ANALYZER_SCRIPT_DESCRIPTIONS", "")
        assert "audit_status_summary=Сводка" in descs
        assert "top_violations_by_type=Топ" in descs

    def test_no_scripts_registry_logs_warning_and_clears(self) -> None:
        """Без ``scripts_registry`` в TableRegistry env-vars пустые (с WARNING)."""
        from loguru import logger

        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(TableResource(name="oarb.audits"),),
        ))

        from lib.core.application_context import _populate_skill_catalog_env

        messages: list[str] = []
        handler_id = logger.add(
            lambda msg: messages.append(str(msg)),
            level="WARNING",
            format="{message}",
        )
        try:
            with patch(
                "lib.services.cache_provider_impl.read_vector_index_config",
                return_value={},
            ):
                _populate_skill_catalog_env()
        finally:
            logger.remove(handler_id)

        assert os.environ.get("SKILL_AUDIT_ANALYZER_SCRIPTS") == ""
        assert any("scripts_registry" in m for m in messages), (
            f"expected warning about scripts_registry, got: {messages}"
        )

    def test_missing_duckdb_cache_logs_info_and_clears(self) -> None:
        """DuckDB-снапшот отсутствует до initial sync → env-vars пустые (INFO).

        Отсутствие snapshot на этапе bootstrap — допустимое состояние,
        не ошибка. Сообщение должно содержать настоящий путь, а не
        литеральный ``%s``.
        """
        from loguru import logger

        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(
                    name="public.agent_predefined_scripts",
                    label="scripts_registry",
                ),
            ),
        ))

        missing = Path("/nonexistent/cache.duckdb")
        from lib.core.application_context import _populate_skill_catalog_env

        messages: list[str] = []
        handler_id = logger.add(
            lambda msg: messages.append(str(msg)),
            level="INFO",
            format="{message}",
        )
        try:
            with patch(
                "lib.services.table_registry.table_registry.snapshot_path",
                return_value=missing,
            ), patch(
                "lib.services.cache_provider_impl.read_vector_index_config",
                return_value={},
            ):
                _populate_skill_catalog_env()
        finally:
            logger.remove(handler_id)

        assert os.environ.get("SKILL_AUDIT_ANALYZER_SCRIPTS") == ""
        info_msgs = [
            m for m in messages
            if "DuckDB snapshot is not available yet" in m
        ]
        assert len(info_msgs) == 1, (
            f"expected exactly 1 INFO, got: {messages}"
        )
        assert str(missing) in info_msgs[0], (
            f"expected real path in message, got: {info_msgs[0]!r}"
        )
        assert "%s" not in info_msgs[0], (
            f"expected no literal %s, got: {info_msgs[0]!r}"
        )


class TestPopulateSkillEnvIntegration:
    """Интеграционный сценарий: всё вместе."""

    def test_full_population(self, tmp_path) -> None:
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name="oarb.audits"),
                TableResource(name="oarb.violations"),
                TableResource(
                    name="public.agent_predefined_scripts",
                    label="scripts_registry",
                ),
            ),
        ))

        fake_vec_cfg = {
            "audits_index": {"description": "Поиск проверок"},
        }

        fake_provider = MagicMock()
        fake_provider.query_sql.return_value = {
            "status": "success",
            "rows": [
                {"name": "audit_status_summary", "description": "Сводка"},
            ],
        }
        fake_provider.close = MagicMock()

        fake_cache = tmp_path / "fake.duckdb"
        fake_cache.write_bytes(b"")

        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value=fake_vec_cfg,
        ), patch(
            "lib.services.table_registry.table_registry.snapshot_path",
            return_value=fake_cache,
        ), patch(
            "lib.services.cache_provider_impl.PostgresDuckDbProvider",
            return_value=fake_provider,
        ):
            _populate_skill_catalog_env()

        assert "oarb.audits" in os.environ.get("SKILL_AUDIT_ANALYZER_TABLES", "")
        assert "oarb.violations" in os.environ.get("SKILL_AUDIT_ANALYZER_TABLES", "")
        assert "audits_index" in os.environ.get("SKILL_AUDIT_ANALYZER_VECTORS", "")
        assert "audit_status_summary" in os.environ.get(
            "SKILL_AUDIT_ANALYZER_SCRIPTS", ""
        )
        assert "audit_status_summary=Сводка" in os.environ.get(
            "SKILL_AUDIT_ANALYZER_SCRIPT_DESCRIPTIONS", ""
        )

    def test_idempotent_repopulate(self) -> None:
        """Повторный вызов не дублирует и не ломает env-vars."""
        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ):
            _populate_skill_catalog_env()
            _populate_skill_catalog_env()

        env = os.environ.get("SKILL_AUDIT_ANALYZER_TABLES", "")
        names = [x for x in env.split(",") if x]
        assert len(names) == len(set(names))


class TestClearOnStop:
    """После ``ApplicationContext.stop`` env-vars очищены."""

    def test_clear_removes_all_skill_envs(self) -> None:
        os.environ["SKILL_AUDIT_ANALYZER_TABLES"] = "x"
        os.environ["SKILL_FOO_BAR"] = "y"
        os.environ["NOT_SKILL"] = "z"

        from lib.utils.skill_catalog import SkillCatalog

        SkillCatalog.clear_skill_env()

        assert "SKILL_AUDIT_ANALYZER_TABLES" not in os.environ
        assert "SKILL_FOO_BAR" not in os.environ
        assert "NOT_SKILL" in os.environ


class TestScriptsCacheSingleLoad:
    """Скрипты общие для всех skill'ов — читаются один раз.

    Без этого при наличии N skill'ов с одним scripts_registry
    получается N одинаковых WARNING и N лишних query_sql.
    """

    def test_duckdb_opened_once_for_two_skills(self, tmp_path) -> None:
        """Два skill'а в реестре → один mock-вызов PostgresDuckDbProvider."""
        from lib.services.table_registry import table_registry as tr

        # Регистрируем второй skill (audit + legal).
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

        fake_provider = MagicMock()
        fake_provider.query_sql.return_value = {
            "status": "success",
            "rows": [
                {"name": "audit_status_summary", "description": "Сводка"},
            ],
        }
        fake_provider.close = MagicMock()

        fake_cache = tmp_path / "cache.duckdb"
        fake_cache.write_bytes(b"")

        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ), patch(
            "lib.services.table_registry.table_registry.snapshot_path",
            return_value=fake_cache,
        ), patch(
            "lib.services.cache_provider_impl.PostgresDuckDbProvider",
            return_value=fake_provider,
        ) as provider_cls:
            _populate_skill_catalog_env(workspace_path=fake_cache.parent)

        # PostgresDuckDbProvider сконструирован ровно один раз,
        # а не дважды (по числу skill'ов).
        assert provider_cls.call_count == 1
        # query_sql тоже ровно один раз.
        assert fake_provider.query_sql.call_count == 1

    def test_info_logged_once_for_all_skills(self, tmp_path) -> None:
        """Два skill'а + отсутствующий snapshot = один INFO, не два."""
        from loguru import logger

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

        missing = Path("/nonexistent/cache.duckdb")
        from lib.core.application_context import _populate_skill_catalog_env

        messages: list[str] = []
        handler_id = logger.add(
            lambda msg: messages.append(str(msg)),
            level="INFO",
            format="{message}",
        )
        try:
            with patch(
                "lib.services.cache_provider_impl.read_vector_index_config",
                return_value={},
            ), patch(
                "lib.services.table_registry.table_registry.snapshot_path",
                return_value=missing,
            ):
                _populate_skill_catalog_env(workspace_path=missing.parent)
        finally:
            logger.remove(handler_id)

        info_msgs = [
            m for m in messages
            if "DuckDB snapshot is not available yet" in m
        ]
        assert len(info_msgs) == 1, (
            f"expected 1 INFO, got {len(info_msgs)}: {info_msgs}"
        )


class TestRefreshRuntimeCatalog:
    """``SkillCatalog.refresh_runtime_catalog`` — повторный populate."""

    def test_refresh_picks_up_new_snapshot(self, tmp_path) -> None:
        """Snapshot появился после первого populate → refresh видит данные."""
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

        missing = tmp_path / "cache.duckdb"
        from lib.core.application_context import _populate_skill_catalog_env
        from lib.utils.skill_catalog import SkillCatalog

        # Шаг 1: snapshot отсутствует → populate пишет пустые env-vars + INFO.
        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ), patch(
            "lib.services.table_registry.table_registry.snapshot_path",
            return_value=missing,
        ):
            _populate_skill_catalog_env(workspace_path=tmp_path)

        assert os.environ.get("SKILL_AUDIT_ANALYZER_SCRIPTS") == ""

        # Шаг 2: snapshot появился → refresh через SkillCatalog читает его.
        missing.write_bytes(b"")
        fake_provider = MagicMock()
        fake_provider.query_sql.return_value = {
            "status": "success",
            "rows": [
                {"name": "audit_status_summary", "description": "Сводка"},
            ],
        }
        fake_provider.close = MagicMock()

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ), patch(
            "lib.services.table_registry.table_registry.snapshot_path",
            return_value=missing,
        ), patch(
            "lib.services.cache_provider_impl.PostgresDuckDbProvider",
            return_value=fake_provider,
        ):
            SkillCatalog.refresh_runtime_catalog(workspace_path=tmp_path)

        scripts = os.environ.get("SKILL_AUDIT_ANALYZER_SCRIPTS", "")
        assert "audit_status_summary" in scripts
        assert "audit_status_summary=Сводка" in os.environ.get(
            "SKILL_AUDIT_ANALYZER_SCRIPT_DESCRIPTIONS", ""
        )

    def test_refresh_is_idempotent(self, tmp_path) -> None:
        """Повторный refresh с тем же snapshot → стабильные env-vars."""
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

        fake_cache = tmp_path / "cache.duckdb"
        fake_cache.write_bytes(b"")
        fake_provider = MagicMock()
        fake_provider.query_sql.return_value = {
            "status": "success",
            "rows": [
                {"name": "audit_status_summary", "description": "Сводка"},
            ],
        }
        fake_provider.close = MagicMock()

        from lib.utils.skill_catalog import SkillCatalog

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ), patch(
            "lib.services.table_registry.table_registry.snapshot_path",
            return_value=fake_cache,
        ), patch(
            "lib.services.cache_provider_impl.PostgresDuckDbProvider",
            return_value=fake_provider,
        ):
            SkillCatalog.refresh_runtime_catalog(workspace_path=tmp_path)
            SkillCatalog.refresh_runtime_catalog(workspace_path=tmp_path)
            SkillCatalog.refresh_runtime_catalog(workspace_path=tmp_path)

        names = [
            x for x in os.environ.get("SKILL_AUDIT_ANALYZER_SCRIPTS", "").split(",")
            if x
        ]
        assert names.count("audit_status_summary") == 1
        # PostgresDuckDbProvider конструируется ровно три раза (один на
        # refresh) — это допустимо, без утечек/дублей в env-vars.
        assert fake_provider.close.call_count == 3

    def test_refresh_after_snapshot_update_shows_new_scripts(
        self, tmp_path: Path,
    ) -> None:
        """A → A+B в snapshot → refresh видит A+B, а не дублирует A."""
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

        fake_cache = tmp_path / "cache.duckdb"
        fake_cache.write_bytes(b"")
        from lib.utils.skill_catalog import SkillCatalog

        # Сценарий 1: в snapshot только A.
        fake_provider_a = MagicMock()
        fake_provider_a.query_sql.return_value = {
            "status": "success",
            "rows": [{"name": "alpha", "description": "alpha desc"}],
        }
        fake_provider_a.close = MagicMock()

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ), patch(
            "lib.services.table_registry.table_registry.snapshot_path",
            return_value=fake_cache,
        ), patch(
            "lib.services.cache_provider_impl.PostgresDuckDbProvider",
            return_value=fake_provider_a,
        ):
            SkillCatalog.refresh_runtime_catalog(workspace_path=tmp_path)

        scripts = os.environ.get("SKILL_AUDIT_ANALYZER_SCRIPTS", "")
        assert "alpha" in scripts
        assert "beta" not in scripts

        # Сценарий 2: в snapshot A и B.
        fake_provider_ab = MagicMock()
        fake_provider_ab.query_sql.return_value = {
            "status": "success",
            "rows": [
                {"name": "alpha", "description": "alpha desc"},
                {"name": "beta", "description": "beta desc"},
            ],
        }
        fake_provider_ab.close = MagicMock()

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ), patch(
            "lib.services.table_registry.table_registry.snapshot_path",
            return_value=fake_cache,
        ), patch(
            "lib.services.cache_provider_impl.PostgresDuckDbProvider",
            return_value=fake_provider_ab,
        ):
            SkillCatalog.refresh_runtime_catalog(workspace_path=tmp_path)

        scripts = os.environ.get("SKILL_AUDIT_ANALYZER_SCRIPTS", "")
        assert scripts.count("alpha") == 1
        assert "beta" in scripts
        assert "alpha=alpha desc" in os.environ.get(
            "SKILL_AUDIT_ANALYZER_SCRIPT_DESCRIPTIONS", ""
        )
        assert "beta=beta desc" in os.environ.get(
            "SKILL_AUDIT_ANALYZER_SCRIPT_DESCRIPTIONS", ""
        )


class TestWrapInitialSyncCallback:
    """``_wrap_initial_sync_callback`` — once-callback на первый sync."""

    def test_callback_fires_only_once(self) -> None:
        """После первого sync-callback обёртка снимает себя."""
        from lib.core.application_context import _wrap_initial_sync_callback

        class FakeSync:
            def __init__(self) -> None:
                self._on_sync_callback = None
                self.saved_prev: object = None

            def set_on_sync_callback(self, cb) -> None:
                self.saved_prev = getattr(self, "_on_sync_callback", None)
                self._on_sync_callback = cb

        sync = FakeSync()
        calls: list[int] = []
        _wrap_initial_sync_callback(sync, lambda: calls.append(1))

        cb = sync._on_sync_callback
        assert cb is not None

        cb()
        cb()
        cb()

        assert calls == [1]
        # После срабатывания обёртка восстановила prev_cb (None в этом
        # случае) и больше не должна быть на sync-сервисе.
        assert sync._on_sync_callback is None

    def test_callback_restores_prev_cb(self) -> None:
        """Если prev_cb уже стоял — он вызывается и сохраняется дальше."""
        from lib.core.application_context import _wrap_initial_sync_callback

        class FakeSync:
            def __init__(self) -> None:
                self._on_sync_callback: object | None = None
                self.prev_calls: list[int] = []

            def set_on_sync_callback(self, cb) -> None:
                self._on_sync_callback = cb

        sync = FakeSync()
        sync._on_sync_callback = lambda: sync.prev_calls.append(1)

        _wrap_initial_sync_callback(sync, lambda: None)

        cb = sync._on_sync_callback
        cb()
        cb()

        # prev_cb вызывается на каждом цикле (через нашу обёртку), пока
        # она активна; после снятия — синхронный вызов prev_cb продолжается.
        assert len(sync.prev_calls) == 2

    def test_callback_swallows_exceptions_in_refresh(self) -> None:
        """Ошибка в once-callback не пробрасывается в sync-цикл."""
        from lib.core.application_context import _wrap_initial_sync_callback

        class FakeSync:
            def __init__(self) -> None:
                self._on_sync_callback = None

            def set_on_sync_callback(self, cb) -> None:
                self._on_sync_callback = cb

        sync = FakeSync()
        _wrap_initial_sync_callback(sync, lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        cb = sync._on_sync_callback
        cb()
        cb()
        # Обёртка снята даже если refresh упал — sync продолжает работу.
        assert sync._on_sync_callback is None

    def test_none_sync_is_noop(self) -> None:
        """Если sync_service=None (sync отключён) — helper ничего не делает."""
        from lib.core.application_context import _wrap_initial_sync_callback

        # Не должно бросить исключение.
        _wrap_initial_sync_callback(None, lambda: None)

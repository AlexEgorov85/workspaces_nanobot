from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.services.session_storage import SessionStorageError, SessionStorageService


@pytest.fixture
def fake_modules():
    """Подменяем модули, от которых SessionStorageService зависит лениво."""
    with patch.dict("sys.modules"):
        fake = {}

        utils = types.ModuleType("utils")
        utils_db = types.ModuleType("utils.db")
        utils_db.configure = MagicMock()
        utils.db = utils_db
        sys.modules["utils"] = utils
        sys.modules["utils.db"] = utils_db
        fake["configure"] = utils_db.configure

        pg_mod = types.ModuleType("lib.session.pg_session_manager")
        pg_mod.PGSessionManager = MagicMock()
        sys.modules["lib.session.pg_session_manager"] = pg_mod
        fake["PGSessionManager"] = pg_mod.PGSessionManager

        nano_session = types.ModuleType("nanobot")
        nano_session.manager = types.ModuleType("nanobot.session.manager")
        nano_session.manager.SessionManager = MagicMock()
        sys.modules["nanobot"] = nano_session
        sys.modules["nanobot.session"] = nano_session.manager
        sys.modules["nanobot.session.manager"] = nano_session.manager
        fake["SessionManager"] = nano_session.manager.SessionManager

        yield fake


def _config(workspace="C:/ws"):
    cfg = MagicMock()
    cfg.workspace_path = Path(workspace)
    return cfg


def _pg(*, dsn="postgresql://u@h/db", **extra):
    cfg = {"dsn": dsn, "messages_table": "agent_session_messages", "meta_table": "agent_session_meta"}
    cfg.update(extra)
    return cfg


class TestCreate:
    def test_postgres_mode_creates_pg_manager(self, fake_modules):
        service = SessionStorageService()
        mode, manager = service.create(
            _config(),
            storage="postgres",
            pg=_pg(),
            configure_db=False,
        )
        assert mode == "postgres"
        fake_modules["PGSessionManager"].assert_called_once()
        assert manager is not None

    def test_auto_with_dsn_creates_pg(self, fake_modules):
        service = SessionStorageService()
        mode, _ = service.create(
            _config(), storage="auto", pg=_pg(), configure_db=False
        )
        assert mode == "postgres"

    def test_auto_without_dsn_returns_file_none(self, fake_modules):
        service = SessionStorageService()
        mode, manager = service.create(_config(), storage="auto", pg={})
        assert mode == "file"
        assert manager is None

    def test_file_mode_ignores_dsn(self, fake_modules):
        service = SessionStorageService()
        mode, manager = service.create(
            _config(), storage="file", pg={"dsn": "postgresql://u@h/db"}, configure_db=False
        )
        assert mode == "file"
        assert manager is None

    def test_file_mode_return_file_manager(self, fake_modules):
        service = SessionStorageService()
        mode, manager = service.create(
            _config(), storage="file", pg={}, return_file_manager=True
        )
        assert mode == "file"
        fake_modules["SessionManager"].assert_called_once()
        assert manager is not None

    def test_postgres_without_dsn_raises(self, fake_modules):
        service = SessionStorageService()
        with pytest.raises(SessionStorageError):
            service.create(_config(), storage="postgres", pg={})

    def test_configure_db_sets_env_and_utils(self, fake_modules):
        service = SessionStorageService()
        with patch.dict("os.environ", clear=True):
            service.create(
                _config(),
                storage="postgres",
                pg=_pg(),
                configure_db=True,
            )
            fake_modules["configure"].assert_called_once_with("postgresql://u@h/db")
            assert os.environ["DATABASE_URL"] == "postgresql://u@h/db"

    def test_workspace_from_config(self, fake_modules):
        service = SessionStorageService()
        service.create(_config("C:/custom/ws"), storage="postgres",
                       pg=_pg(), configure_db=False)
        kwargs = fake_modules["PGSessionManager"].call_args.kwargs
        assert kwargs["workspace"] == Path("C:/custom/ws")


class TestSessionManagerJsonOverride:
    """Тесты override из session_manager.json теперь живут в Resolver
    (см. config._load_session_manager_override). SessionStorageService
    больше не читает этот файл — параметр ``session_manager_json``
    удалён из конструктора.
    """

    def test_override_wins_over_pg(self, fake_modules, tmp_path):
        """Прежнее поведение переехало в config.resolve_application_config —
        см. tests/test_config_resolver.py::test_session_manager_can_override_pool_but_not_runtime_tables.
        Этот тест оставлен как smoke-проверка, что SessionStorageService
        принимает ``pg`` с уже разрешённым override (без собственного
        чтения session_manager.json)."""
        service = SessionStorageService()
        service.create(
            _config(),
            storage="postgres",
            pg=_pg(
                dsn="postgresql://override/db",
                schema="custom",
                max_conn=8,
            ),
            configure_db=False,
        )
        kwargs = fake_modules["PGSessionManager"].call_args.kwargs
        assert kwargs["dsn"] == "postgresql://override/db"
        assert kwargs["schema"] == "custom"
        assert kwargs["max_conn"] == 8

    def test_missing_json_is_ignored(self, fake_modules, tmp_path):
        """Без session_manager.json работает — override пустой."""
        service = SessionStorageService()
        mode, _ = service.create(
            _config(), storage="postgres",
            pg=_pg(), configure_db=False,
        )
        assert mode == "postgres"

    def test_invalid_json_raises(self, fake_modules, tmp_path, monkeypatch):
        """Невалидный JSON в session_manager.json → ValueError из Resolver.

        Подменяем _SESSION_MANAGER_FILE в config на путь с битым JSON."""
        sm_json = tmp_path / "session_manager.json"
        sm_json.write_text("{broken json", encoding="utf-8")
        import config as config_mod
        monkeypatch.setattr(config_mod, "_SESSION_MANAGER_FILE", sm_json)

        with pytest.raises(ValueError):
            config_mod._load_session_manager_override()

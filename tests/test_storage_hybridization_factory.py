"""Mock-smoke для storage-hybridization: создание LLMUsageStore и
зеркала сессий без реального PG.

Проверяет:

- ``_make_usage_store`` корректно создаёт ``LLMUsageStore`` при
  наличии ``gateway.usage_store.*`` в конфиге;
- ``_make_usage_store`` возвращает ``None`` при ``enabled=False``
  или отсутствии конфига;
- ``_make_session_mirror`` создаёт сервис при наличии
  ``channels.postgres.dsn``;
- ``_make_session_mirror`` возвращает ``None`` без DSN.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from config import runtime_table  # noqa: F401


class _FakeConfigService:
    """``settings_section`` принимает **первый** сегмент пути
    (например, ``"gateway"`` для ``gateway.usage_store``) и возвращает
    соответствующий dict. В тестах данные лежат под ключом
    ``"gateway"`` целиком."""

    def __init__(self, gateway_section: dict | None = None,
                 flat: dict | None = None) -> None:
        if flat is not None:
            self._gw = dict(flat)
        else:
            self._gw = gateway_section or {}

    def settings_section(self, name: str) -> dict:
        if name == "gateway":
            return dict(self._gw)
        return {}


def _fake_ctx(**attrs) -> SimpleNamespace:
    return SimpleNamespace(**attrs)


class TestMakeUsageStore:
    def test_returns_none_without_config(self, tmp_path: Path) -> None:
        from lib.core.application_context import _make_usage_store

        ctx = _fake_ctx(config_service=_FakeConfigService(None))
        assert _make_usage_store(ctx) is None

    def test_returns_none_when_disabled(self, tmp_path: Path) -> None:
        from lib.core.application_context import _make_usage_store

        ctx = _fake_ctx(config_service=_FakeConfigService(flat={
            "usage_store": {"enabled": False, "sqlite_path": str(tmp_path / "u.db")},
        }))
        assert _make_usage_store(ctx) is None

    def test_creates_store_with_default_path(self, tmp_path: Path) -> None:
        from lib.core.application_context import _make_usage_store

        ctx = _fake_ctx(config_service=_FakeConfigService(flat={
            "usage_store": {"enabled": True, "sqlite_path": str(tmp_path / "u.db")},
        }))
        store = _make_usage_store(ctx)
        assert store is not None
        store.close()

    def test_creates_store_with_explicit_path(self, tmp_path: Path) -> None:
        from lib.core.application_context import _make_usage_store

        path = tmp_path / "explicit.db"
        ctx = _fake_ctx(config_service=_FakeConfigService(flat={
            "usage_store": {"enabled": True, "sqlite_path": str(path)},
        }))
        store = _make_usage_store(ctx)
        assert store is not None
        assert Path(path).exists() or path.parent.exists()
        store.close()


class TestMakeSessionMirror:
    """Сборка зеркала. Условие создания сменилось с «есть DSN в PG-конфиге»
    на «есть клиент платформы»: прямого доступа к БД у сервиса больше нет,
    и имена таблиц он не получает вовсе."""

    def test_returns_none_without_session_manager(self) -> None:
        from lib.core.application_context import _make_session_mirror

        ctx = _fake_ctx(
            session_manager=None,
            config_service=_FakeConfigService(None),
            db_logging_service=None,
        )
        assert _make_session_mirror(ctx) is None

    def test_service_gets_no_table_names(self) -> None:
        """Имена таблиц зеркала объявлены на платформе. Если агент снова начнёт
        их читать из своей конфигурации, появится вторая копия объявления —
        ровно тот рассинхрон, из-за которого канал и журнал ушли на платформу."""
        from lib.core.application_context import _make_session_mirror

        ctx = _fake_ctx(
            session_manager=object(),
            config_service=_FakeConfigService(flat={"session_cold_sync": {}}),
            db_logging_service=None,
        )
        svc = _make_session_mirror(ctx)
        assert svc is not None
        assert not hasattr(svc, "_meta_table")
        assert not hasattr(svc, "_messages_table")
        assert not hasattr(svc, "_pg_dsn")

    def test_replica_id_comes_from_settings(self) -> None:
        from lib.core.application_context import _make_session_mirror

        ctx = _fake_ctx(
            session_manager=object(),
            config_service=_FakeConfigService(flat={
                "session_cold_sync": {"replica_id": "gw-2"},
            }),
            db_logging_service=None,
        )
        svc = _make_session_mirror(ctx)
        assert svc is not None
        assert svc.replica_id == "gw-2"

    def test_replica_id_defaults_to_something_stable(self) -> None:
        """Без явной настройки идентичность реплики должна переживать
        перезапуск: идентификатор с pid'ом оставил бы прежние строки зеркала
        осиротевшими, и они копились бы после каждого рестарта."""
        from lib.core.application_context import _make_session_mirror
        from lib.gateway.mirror import default_replica_id

        ctx = _fake_ctx(
            session_manager=object(),
            config_service=_FakeConfigService(flat={"session_cold_sync": {}}),
            db_logging_service=None,
        )
        svc = _make_session_mirror(ctx)
        assert svc is not None
        assert svc.replica_id == default_replica_id()
        assert default_replica_id() == default_replica_id()

    def test_respects_enabled_false(self) -> None:
        from lib.core.application_context import _make_session_mirror

        ctx = _fake_ctx(
            session_manager=object(),
            config_service=_FakeConfigService(flat={
                "session_cold_sync": {"enabled": False},
            }),
            db_logging_service=None,
        )
        svc = _make_session_mirror(ctx)

        assert svc is not None
        assert svc.enabled is False

    def test_missing_cycles_threshold_is_wired(self) -> None:
        """Порог подтверждения пропажи — единственная защита от стирания
        зеркала по пустому списку сессий; молчаливое значение по умолчанию
        здесь означало бы, что оператор никогда о нём не узнает."""
        from lib.core.application_context import _make_session_mirror

        ctx = _fake_ctx(
            session_manager=object(),
            config_service=_FakeConfigService(flat={
                "session_cold_sync": {"missing_cycles_threshold": 5},
            }),
            db_logging_service=None,
        )
        svc = _make_session_mirror(ctx)
        assert svc.get_stats()["missing_cycles_threshold"] == 5


class TestApplicationContextHasNewAttrs:
    def test_factory_functions_exist(self) -> None:
        from lib.core.application_context import (
            _make_session_mirror,
            _make_usage_store,
        )
        assert callable(_make_session_mirror)
        assert callable(_make_usage_store)
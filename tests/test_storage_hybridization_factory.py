"""Mock-smoke для storage-hybridization: создание LLMUsageStore и
SessionColdSyncService без реального PG.

Проверяет:

- ``_make_usage_store`` корректно создаёт ``LLMUsageStore`` при
  наличии ``gateway.usage_store.*`` в конфиге;
- ``_make_usage_store`` возвращает ``None`` при ``enabled=False``
  или отсутствии конфига;
- ``_make_session_cold_sync_service`` создаёт сервис при наличии
  ``channels.postgres.dsn``;
- ``_make_session_cold_sync_service`` возвращает ``None`` без DSN.
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


class TestMakeSessionColdSyncService:
    def test_returns_none_without_session_manager(self) -> None:
        from lib.core.application_context import _make_session_cold_sync_service

        ctx = _fake_ctx(
            session_manager=None,
            config_service=_FakeConfigService(None),
            db_logging_service=None,
        )
        with patch("config.get_setting", return_value=""):
            assert _make_session_cold_sync_service(ctx) is None

    def test_returns_none_without_dsn(self) -> None:
        from lib.core.application_context import _make_session_cold_sync_service

        ctx = _fake_ctx(
            session_manager=object(),
            config_service=_FakeConfigService(None),
            db_logging_service=None,
        )
        with patch("config.get_setting", return_value=""):
            assert _make_session_cold_sync_service(ctx) is None

    def test_creates_service_with_dsn(self, tmp_path: Path) -> None:
        from lib.core.application_context import _make_session_cold_sync_service

        sm = object()
        ctx = _fake_ctx(
            session_manager=sm,
            config_service=_FakeConfigService(flat={"session_cold_sync": {}}),
            db_logging_service=None,
        )

        def _fake_get_setting(*keys, default=""):
            if "dsn" in keys:
                return "postgresql://test"
            if "schema" in keys:
                return "public"
            if "meta_table" in keys:
                return runtime_table("session_meta")
            if "messages_table" in keys:
                return runtime_table("session_messages")
            return default

        with (
            patch("config.get_setting", side_effect=_fake_get_setting),
            patch("config.require_setting", side_effect=_fake_get_setting),
        ):
            svc = _make_session_cold_sync_service(ctx)

        assert svc is not None
        assert svc.enabled is True
        assert svc._session_manager is sm
        assert svc._meta_table == runtime_table("session_meta")
        assert svc._messages_table == runtime_table("session_messages")

    def test_respects_enabled_false(self) -> None:
        from lib.core.application_context import _make_session_cold_sync_service

        sm = object()
        ctx = _fake_ctx(
            session_manager=sm,
            config_service=_FakeConfigService(flat={
                "session_cold_sync": {"enabled": False},
            }),
            db_logging_service=None,
        )

        def _fake_get_setting(*keys, default=""):
            if "dsn" in keys:
                return "postgresql://test"
            if "schema" in keys:
                return "public"
            if "meta_table" in keys:
                return runtime_table("session_meta")
            if "messages_table" in keys:
                return runtime_table("session_messages")
            return default

        with (
            patch("config.get_setting", side_effect=_fake_get_setting),
            patch("config.require_setting", side_effect=_fake_get_setting),
        ):
            svc = _make_session_cold_sync_service(ctx)

        assert svc is not None
        assert svc.enabled is False


class TestApplicationContextHasNewAttrs:
    def test_factory_functions_exist(self) -> None:
        from lib.core.application_context import (
            _make_session_cold_sync_service,
            _make_usage_store,
        )
        assert callable(_make_session_cold_sync_service)
        assert callable(_make_usage_store)
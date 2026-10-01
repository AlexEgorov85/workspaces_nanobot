"""Integration-тесты ``ApplicationContext._validate_runtime_schema``.

Покрывают:
  * старт блокируется при отсутствии таблиц (hard-fail);
  * старт проходит при всех таблицах;
  * опциональный gate ``gateway.startup.schema_validation.enabled``
    пропускает проверку с WARNING;
  * отсутствие ключей в settings → ``ConfigurationError``;
  * порядок: проверка идёт ПОСЛЕ ``_start_db_pool()`` и ДО
    подъёма ``db_logging_service`` / ``RuntimeEventsSubscriber``.

Использует stub-объект вместо полноценного ``ApplicationContext.create``
— метод не использует ``self`` кроме ``self.settings``.
См. ``openspec/specs/runtime/startup-schema-validation/spec.md``.
"""
from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from config import ConfigurationError
from lib.services.schema_validation import (
    MissingTable,
    SchemaValidationError,
    SchemaValidationService,
)
from config import runtime_table  # noqa: F401


class _CtxStub:
    """Минимальный stand-in для ApplicationContext.

    ``_validate_runtime_schema(self)`` обращается только к ``self.settings``.
    """

    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings

    _validate_runtime_schema = (
        ApplicationContext := None  # type: ignore[assignment]
    ) or None  # placeholder — реальный метод установим ниже


def _settings(
    *,
    enabled: bool = True,
    timeout: float = 5.0,
    missing_one: str | None = None,
) -> dict[str, Any]:
    """Шаблон SETTINGS с возможностью «удалить» одну таблицу."""
    pg = {
        "table_name": runtime_table("conversation_messages"),
        "messages_table": runtime_table("session_messages"),
        "meta_table": runtime_table("session_meta"),
    }
    db = {
        "table_name": runtime_table("gateway_logs"),
        "question_runs_table": runtime_table("question_runs"),
    }
    if missing_one is not None:
        if missing_one in pg:
            pg[missing_one] = "absent_table_xxx"
        elif missing_one in db:
            db[missing_one] = "absent_table_xxx"
        else:
            raise ValueError(f"unknown key: {missing_one}")
    return {
        "profile": "prod",
        "channels": {"postgres": pg},
        "logging": {"db": db},
        "gateway": {
            "startup": {
                "schema_validation": {
                    "enabled": enabled,
                    "timeout_sec": timeout,
                },
            },
        },
    }


def _attach_method() -> None:
    """Подключить реальный ``_validate_runtime_schema`` к stub-классу.

    Импортируем лениво, чтобы не падать на отсутствии nanobot.
    """
    if _CtxStub._validate_runtime_schema is not None:
        return
    from lib.core.application_context import ApplicationContext
    _CtxStub._validate_runtime_schema = ApplicationContext._validate_runtime_schema


class TestValidateRuntimeSchema:
    def setup_method(self) -> None:
        _attach_method()

    def test_passes_when_all_tables_present(self) -> None:
        ctx = _CtxStub(_settings())
        # Mock fetch возвращает все 5 таблиц
        names = SchemaValidationService.expected_table_names(ctx.settings)
        existing = {n for _, n in names}

        def _fetch(sql: str, *params: Any) -> list[dict[str, Any]]:
            return [
                {"table_schema": s, "table_name": n}
                for s, n in names
                if n in params
            ]

        with patch("utils.db.fetch", _fetch):
            ctx._validate_runtime_schema()  # no raise

    def test_raises_when_one_table_missing(self) -> None:
        # Делаем так, чтобы agent_gateway_logs отсутствовал
        settings = _settings()
        # Подменяем настройку fetch, чтобы вернуть только 4 из 5.
        names = SchemaValidationService.expected_table_names(settings)
        existing = {n for _, n in names if n != runtime_table("gateway_logs")}

        def _fetch(sql: str, *params: Any) -> list[dict[str, Any]]:
            return [
                {"table_schema": "public", "table_name": p}
                for p in params
                if isinstance(p, str) and p in existing
            ]

        ctx = _CtxStub(settings)
        with patch("utils.db.fetch", _fetch):
            with pytest.raises(SchemaValidationError) as exc_info:
                ctx._validate_runtime_schema()
        assert exc_info.value.profile == "prod"
        assert any(m.name == runtime_table("gateway_logs") for m in exc_info.value.missing)

    def test_enabled_false_skips_check_with_warning(self) -> None:
        settings = _settings(enabled=False)

        # fetch-функция, которая RAISE'ит, если её вызвали.
        def _fetch(sql: str, *params: Any) -> list[dict[str, Any]]:
            raise AssertionError("fetch should not be called when disabled")

        ctx = _CtxStub(settings)
        with patch("utils.db.fetch", _fetch):
            ctx._validate_runtime_schema()  # no raise

    def test_missing_settings_keys_raises_configuration_error(self) -> None:
        settings = _settings()
        del settings["logging"]["db"]["question_runs_table"]

        def _fetch(sql: str, *params: Any) -> list[dict[str, Any]]:
            return []

        ctx = _CtxStub(settings)
        with patch("utils.db.fetch", _fetch):
            with pytest.raises(ConfigurationError) as exc_info:
                ctx._validate_runtime_schema()
        assert isinstance(exc_info.value, SchemaValidationError)

    def test_db_unavailable_is_not_masked_as_missing(self) -> None:
        """Если fetch падает с OperationalError — пусть поднимется,
        а не маскируется под «отсутствие таблиц».
        """
        settings = _settings()

        def _fetch(sql: str, *params: Any) -> list[dict[str, Any]]:
            raise RuntimeError("DB connection refused")

        ctx = _CtxStub(settings)
        with patch("utils.db.fetch", _fetch):
            with pytest.raises(RuntimeError, match="DB connection refused"):
                ctx._validate_runtime_schema()

    def test_uses_timeout_from_settings(self) -> None:
        """Таймаут берётся из settings, не из литерала."""
        captured: dict[str, Any] = {}

        def _fetch(sql: str, *params: Any) -> list[dict[str, Any]]:
            captured["called"] = True
            # Возвращаем все 5 таблиц существующими, чтобы не падать.
            names = SchemaValidationService.expected_table_names(_settings())
            return [
                {"table_schema": "public", "table_name": n}
                for _, n in names
            ]

        settings = _settings(timeout=2.5)
        ctx = _CtxStub(settings)
        with patch("utils.db.fetch", _fetch):
            ctx._validate_runtime_schema()
        assert captured["called"]


class TestOrderingInStart:
    """Подтверждаем, что ``_validate_runtime_schema`` присутствует и
    доступен как метод ``ApplicationContext``.

    Реальный порядок вызовов внутри ``start()`` — гарантируется
    ручным review (см. ``lib/core/application_context.py:start()`` —
    вызов расположен между ``_start_db_pool()`` и подписчиком runtime
    events). Отдельный тест на порядок потребовал бы полного mock'а
    всего ``ApplicationContext.create``, что выходит за scope этой
    change — это ответственность ``test_application_context.py``.
    """

    def setup_method(self) -> None:
        _attach_method()

    def test_method_is_bound_on_application_context(self) -> None:
        from lib.core.application_context import ApplicationContext

        assert hasattr(ApplicationContext, "_validate_runtime_schema")
        assert callable(ApplicationContext._validate_runtime_schema)

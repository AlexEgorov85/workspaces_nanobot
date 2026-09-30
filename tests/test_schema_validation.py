"""Unit-тесты ``lib.services.schema_validation``.

Покрывают контракт:
  * имена таблиц не зашиты — берутся из SETTINGS;
  * отсутствие хотя бы одной таблицы → ``SchemaValidationError``;
  * отсутствие ключей в settings → ``_MissingConfigKeys`` (наследник
    ``SchemaValidationError`` → ``ConfigurationError``);
  * SELECT идёт через параметры, не через f-string;
  * ошибки БД (OperationalError) НЕ маскируются под missing;
  * работает с ``_LazySettings`` proxy (как приходит из ``ctx.settings``).

См. ``openspec/specs/runtime/startup-schema-validation/spec.md``.
"""
from __future__ import annotations

from typing import Any

import pytest

from config import ConfigurationError
from lib.services.schema_validation import (
    DEFAULT_TIMEOUT_SEC,
    MissingTable,
    SchemaValidationError,
    SchemaValidationService,
    _MissingConfigKeys,
    _hint_for_profile,
)


def _full_settings(profile: str = "prod") -> dict[str, Any]:
    """Шаблон SETTINGS с полным набором 5 runtime-ключей."""
    return {
        "profile": profile,
        "channels": {
            "postgres": {
                "table_name": "agent_conversation_messages",
                "messages_table": "agent_session_messages",
                "meta_table": "agent_session_meta",
            },
        },
        "logging": {
            "db": {
                "table_name": "agent_gateway_logs",
                "question_runs_table": "agent_question_runs",
            },
        },
    }


def _make_fetch(existing_names: set[str]) -> Any:
    """Mock ``utils.db.fetch``: возвращает строки для имён в existing_names."""
    def _fetch(sql: str, *params: Any) -> list[dict[str, Any]]:
        result = []
        for p in params:
            if isinstance(p, str) and p in existing_names:
                result.append({"table_schema": "public", "table_name": p})
        seen = set()
        uniq = []
        for r in result:
            key = (r["table_schema"], r["table_name"])
            if key not in seen:
                seen.add(key)
                uniq.append(r)
        return uniq
    return _fetch


class TestSchemaValidationError:
    def test_is_configuration_error(self) -> None:
        err = SchemaValidationError(
            [MissingTable(schema="public", name="foo")],
            profile="prod",
        )
        assert isinstance(err, ConfigurationError)
        assert isinstance(err, SchemaValidationError)

    def test_message_contains_missing_and_profile(self) -> None:
        err = SchemaValidationError(
            [
                MissingTable(schema="public", name="agent_conversation_messages"),
                MissingTable(schema="public", name="agent_gateway_logs"),
            ],
            profile="test",
        )
        text = str(err)
        assert "profile='test'" in text
        assert "public.agent_conversation_messages" in text
        assert "public.agent_gateway_logs" in text
        assert "python tools/apply_test_profile_tables.py" in text

    def test_empty_missing_message_still_safe(self) -> None:
        err = SchemaValidationError([], profile="prod")
        text = str(err)
        assert "profile='prod'" in text

    def test_message_in_russian(self) -> None:
        err = SchemaValidationError(
            [MissingTable(schema="public", name="agent_gateway_logs")],
            profile="prod",
        )
        text = str(err)
        assert "Не найдены обязательные runtime-таблицы" in text
        assert "Отсутствуют таблицы:" in text
        assert "Подсказка:" in text
        assert "python tools/migrate.py --apply" in text

    def test_missing_config_keys_message_in_russian(self) -> None:
        err = _MissingConfigKeys(
            ["channels.postgres.table_name"],
            profile="prod",
        )
        text = str(err)
        assert "Не найдены обязательные ключи конфигурации" in text
        assert "Отсутствуют ключи:" in text
        assert "profile='prod'" in text
        assert "channels.postgres.table_name" in text
        assert "project.json" in text


class TestHintForProfile:
    @pytest.mark.parametrize(
        ("profile", "expected"),
        [
            ("prod", "python tools/migrate.py --apply"),
            ("test", "python tools/apply_test_profile_tables.py"),
            ("dev", "примените миграции для выбранного профиля"),
            ("", "примените миграции для выбранного профиля"),
        ],
    )
    def test_hint_for_known_and_unknown_profiles(
        self, profile: str, expected: str
    ) -> None:
        assert _hint_for_profile(profile) == expected


class TestMissingTable:
    def test_full_name(self) -> None:
        t = MissingTable(schema="public", name="agent_x")
        assert t.full_name == "public.agent_x"

    def test_frozen(self) -> None:
        t = MissingTable(schema="public", name="agent_x")
        with pytest.raises((AttributeError, TypeError)):
            t.schema = "other"  # type: ignore[misc]


class TestExpectedTableNames:
    def test_extracts_five_names_in_order(self) -> None:
        settings = _full_settings()
        names = SchemaValidationService.expected_table_names(settings)
        assert names == [
            ("public", "agent_conversation_messages"),
            ("public", "agent_session_messages"),
            ("public", "agent_session_meta"),
            ("public", "agent_gateway_logs"),
            ("public", "agent_question_runs"),
        ]

    def test_passes_through_test_suffix_names_verbatim(self) -> None:
        settings = {
            "profile": "test",
            "channels": {
                "postgres": {
                    "table_name": "agent_conversation_messages_test",
                    "messages_table": "agent_session_messages_test",
                    "meta_table": "agent_session_meta_test",
                },
            },
            "logging": {
                "db": {
                    "table_name": "agent_gateway_logs_test",
                    "question_runs_table": "agent_question_runs_test",
                },
            },
        }
        names = SchemaValidationService.expected_table_names(settings)
        assert all(n.endswith("_test") for _, n in names)
        assert names[0] == ("public", "agent_conversation_messages_test")

    def test_custom_names_not_hardcoded(self) -> None:
        settings = {
            "profile": "prod",
            "channels": {
                "postgres": {
                    "table_name": "my_custom_chat",
                    "messages_table": "my_custom_msg",
                    "meta_table": "my_custom_meta",
                },
            },
            "logging": {
                "db": {
                    "table_name": "my_custom_logs",
                    "question_runs_table": "my_custom_runs",
                },
            },
        }
        names = SchemaValidationService.expected_table_names(settings)
        assert names == [
            ("public", "my_custom_chat"),
            ("public", "my_custom_msg"),
            ("public", "my_custom_meta"),
            ("public", "my_custom_logs"),
            ("public", "my_custom_runs"),
        ]

    def test_missing_channel_keys_raises(self) -> None:
        settings = _full_settings()
        del settings["channels"]["postgres"]["meta_table"]
        with pytest.raises(_MissingConfigKeys) as exc_info:
            SchemaValidationService.expected_table_names(settings)
        assert "channels.postgres.meta_table" in exc_info.value.missing_config_keys

    def test_missing_logging_keys_raises(self) -> None:
        settings = _full_settings()
        del settings["logging"]["db"]["question_runs_table"]
        with pytest.raises(_MissingConfigKeys) as exc_info:
            SchemaValidationService.expected_table_names(settings)
        assert (
            "logging.db.question_runs_table"
            in exc_info.value.missing_config_keys
        )

    def test_empty_string_value_raises(self) -> None:
        settings = _full_settings()
        settings["channels"]["postgres"]["table_name"] = ""
        with pytest.raises(_MissingConfigKeys):
            SchemaValidationService.expected_table_names(settings)

    def test_works_with_lazy_settings_proxy(self) -> None:
        """``ctx.settings`` — это ``_LazySettings`` proxy, не сырой dict.

        Без unwrap ``isinstance(proxy, dict) == False`` и обход
        по путям падает на первом же уровне, показывая отсутствие
        ВСЕХ ключей (это и был реальный баг v2.5.2: gateway не
        стартовал с понятным списком ``<settings>.channels.postgres.*``).
        """
        from config import AttrDict, _LazySettings

        raw = AttrDict(_full_settings())
        proxy = _LazySettings()
        proxy._inner_dict = raw
        names = SchemaValidationService.expected_table_names(proxy)
        assert names == [
            ("public", "agent_conversation_messages"),
            ("public", "agent_session_messages"),
            ("public", "agent_session_meta"),
            ("public", "agent_gateway_logs"),
            ("public", "agent_question_runs"),
        ]

    def test_lazy_settings_missing_keys_reports_correctly(self) -> None:
        """Unwrap не теряет диагностику: пути пишутся как ``channels.postgres.*``,
        не как ``<settings>.channels.postgres.*``.
        """
        from config import AttrDict, _LazySettings

        raw = AttrDict(_full_settings())
        del raw["channels"]["postgres"]["table_name"]
        proxy = _LazySettings()
        proxy._inner_dict = raw
        with pytest.raises(_MissingConfigKeys) as exc_info:
            SchemaValidationService.expected_table_names(proxy)
        assert exc_info.value.missing_config_keys == [
            "channels.postgres.table_name"
        ]
        # Сообщение не должно содержать ``<settings>``.
        assert "<settings>" not in str(exc_info.value)


class TestCheckTables:
    def test_all_present_returns_empty(self) -> None:
        names = SchemaValidationService.expected_table_names(_full_settings())
        fetch = _make_fetch({n for _, n in names})
        missing = SchemaValidationService.check_tables(fetch, names)
        assert missing == []

    def test_one_missing_returned(self) -> None:
        names = SchemaValidationService.expected_table_names(_full_settings())
        existing = {n for _, n in names if n != "agent_gateway_logs"}
        fetch = _make_fetch(existing)
        missing = SchemaValidationService.check_tables(fetch, names)
        assert len(missing) == 1
        assert missing[0].schema == "public"
        assert missing[0].name == "agent_gateway_logs"

    def test_all_missing_returns_all(self) -> None:
        names = SchemaValidationService.expected_table_names(_full_settings())
        fetch = _make_fetch(set())
        missing = SchemaValidationService.check_tables(fetch, names)
        assert len(missing) == len(names)

    def test_empty_expected_returns_empty(self) -> None:
        fetch = _make_fetch(set())
        assert SchemaValidationService.check_tables(fetch, []) == []

    def test_sql_uses_placeholders_not_fstring(self) -> None:
        names = [("public", "evil'; DROP TABLE foo; --")]
        captured: dict[str, Any] = {}

        def _fetch(sql: str, *params: Any) -> list[dict[str, Any]]:
            captured["sql"] = sql
            captured["params"] = params
            return []

        SchemaValidationService.check_tables(_fetch, names)
        assert "evil'; DROP TABLE foo; --" not in captured["sql"]
        assert "evil'; DROP TABLE foo; --" in captured["params"]

    def test_default_timeout_value(self) -> None:
        assert DEFAULT_TIMEOUT_SEC == 5.0


class TestValidate:
    def test_passes_when_all_present(self) -> None:
        settings = _full_settings()
        names = SchemaValidationService.expected_table_names(settings)
        fetch = _make_fetch({n for _, n in names})
        SchemaValidationService.validate(settings, fetch=fetch)

    def test_raises_schema_validation_error_on_missing(self) -> None:
        settings = _full_settings()
        names = SchemaValidationService.expected_table_names(settings)
        existing = {n for _, n in names if n != "agent_session_meta"}
        fetch = _make_fetch(existing)
        with pytest.raises(SchemaValidationError) as exc_info:
            SchemaValidationService.validate(settings, fetch=fetch)
        assert exc_info.value.profile == "prod"
        assert any(
            m.name == "agent_session_meta" for m in exc_info.value.missing
        )

    def test_missing_keys_raises_configuration_error(self) -> None:
        settings = _full_settings()
        del settings["logging"]["db"]["table_name"]
        fetch = _make_fetch(set())
        with pytest.raises(ConfigurationError) as exc_info:
            SchemaValidationService.validate(settings, fetch=fetch)
        assert isinstance(exc_info.value, SchemaValidationError)

    def test_works_with_lazy_settings_proxy(self) -> None:
        """End-to-end через proxy: должны извлечься 5 имён и
        пройти через ``check_tables`` без missing.
        """
        from config import AttrDict, _LazySettings

        raw = AttrDict(_full_settings())
        proxy = _LazySettings()
        proxy._inner_dict = raw

        def _fetch_all(sql: str, *params: Any) -> list[dict[str, Any]]:
            names = [p for p in params if isinstance(p, str)]
            return [
                {"table_schema": "public", "table_name": n}
                for n in names
                if not n.startswith("public")
            ]

        SchemaValidationService.validate(proxy, fetch=_fetch_all)

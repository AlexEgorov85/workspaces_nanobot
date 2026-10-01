"""Обязательные runtime-таблицы доезжают до операции ``schema_check``.

Дефект, который закрывает этот файл
-----------------------------------

``schema_check`` — единственная операция платформы, которая спрашивает
PostgreSQL «таблицы на месте?». Агент знает их имена
(``SchemaValidationService`` блокирует старт по тому же списку), но не
передавал их в процесс сервера, и операция отвечала
``[invalid_request] не задано ни одной ожидаемой таблицы``.

Хуже всего, что это выглядело как «таблиц нет». На деле отсутствие списка
означало, что проверить нечего, и операция молча уводила от реальной
проверки схемы. Ровно тот класс отказа, что и в ``audit``: возможность
связана, но неработоспособна, а юнит-тесты зелёные, потому что они сами
передают список.

Список приходит **аргументом**, а не вычисляется в клиенте: единственный
источник — ``SchemaValidationService.expected_table_names``, и копия
разошлась бы с ним при первой правке.
"""

from __future__ import annotations

from typing import Any

from config import runtime_table
from lib.core import application_context as ac
from lib.services import enterprise_mcp_client as mod

EXPECTED_KEYS = (
    ("channels", "postgres", "table_name"),
    ("channels", "postgres", "messages_table"),
    ("channels", "postgres", "meta_table"),
    ("logging", "db", "table_name"),
    ("logging", "db", "question_runs_table"),
)


def _rt(role: str) -> str:
    """``schema.имя`` из настроек — литерал таблицы здесь не нужен."""
    return f"public.{runtime_table(role)}"


def _settings(**overrides: Any) -> dict:
    base = {
        "channels": {
            "postgres": {
                "table_name": _rt("conversation_messages"),
                "messages_table": _rt("session_messages"),
                "meta_table": _rt("session_meta"),
            }
        },
        "logging": {
            "db": {
                "table_name": _rt("gateway_logs"),
                "question_runs_table": _rt("question_runs"),
            }
        },
    }
    for key, value in overrides.items():
        base[key] = value
    return base


class _Ctx:
    def __init__(self, settings: Any) -> None:
        self.settings = settings


class TestExpectedTablesResolve:
    def test_names_are_schema_qualified(self) -> None:
        tables = ac._expected_tables(_Ctx(_settings()))
        assert tables == (
            _rt("conversation_messages"),
            _rt("session_messages"),
            _rt("session_meta"),
            _rt("gateway_logs"),
            _rt("question_runs"),
        )

    def test_every_expected_key_is_covered(self) -> None:
        """Список не должен разъехаться с тем, чем блокируется старт."""
        from lib.services.schema_validation import _EXPECTED_KEYS

        assert _EXPECTED_KEYS == EXPECTED_KEYS, (
            "тест разошёлся с _EXPECTED_KEYS из schema_validation — "
            "проверяется не то же, что проверяет старт агента"
        )
        assert len(ac._expected_tables(_Ctx(_settings()))) == len(_EXPECTED_KEYS)

    def test_missing_config_does_not_break_startup(self) -> None:
        """Нет настроек — пустой список, а не исключение на старте."""
        assert ac._expected_tables(_Ctx({})) == ()

    def test_absent_ctx_is_tolerated(self) -> None:
        assert ac._expected_tables(None) == ()

    def test_partial_config_yields_empty_list_not_crash(self) -> None:
        """Неполная конфигурация даёт пустой список и предупреждение, а не падение.

        Раньше здесь поднимался ``_MissingConfigKeys``; обёртка его ловит,
        чтобы неполный конфиг не ронял старт агента из-за проверки, которая
        для старта не обязательна.
        """
        broken = {"channels": {"postgres": {"table_name": "public.only_one"}}}
        assert ac._expected_tables(_Ctx(broken)) == ()

    def test_qualify_does_not_double_the_schema(self) -> None:
        """Резолвер агента отдаёт уже квалифицированное имя — схему не дублируем."""
        assert ac._qualify("public", "public.agent_x") == "public.agent_x"

    def test_qualify_adds_schema_when_absent(self) -> None:
        assert ac._qualify("oarb", "audits") == "oarb.audits"

    def test_qualify_without_schema_returns_name(self) -> None:
        assert ac._qualify("", "audits") == "audits"


class TestExpectedTablesReachChildEnv:
    def test_exported_as_comma_separated(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "_vectors_env_from_settings", lambda _p: {})
        monkeypatch.setattr(mod, "_audit_env_from_project", dict)
        client = mod.EnterpriseMcpClient(
            command="python",
            expected_tables=("public.a", "public.b"),
        )
        env = client._child_env()
        assert env["ENTERPRISE_EXPECTED_TABLES"] == "public.a,public.b"

    def test_empty_list_is_not_exported(self, monkeypatch) -> None:
        """Пустой список не выгружается: «не задано» и «пусто» — разные вещи.

        Пустая строка в окружении дала бы платформе «оператор ничего не
        объявил», отсутствие переменной — «список не передан вовсе». Разница
        видна только в ответе операции, и она полезна при разборе.
        """
        monkeypatch.setattr(mod, "_vectors_env_from_settings", lambda _p: {})
        monkeypatch.setattr(mod, "_audit_env_from_project", dict)
        client = mod.EnterpriseMcpClient(command="python", expected_tables=())
        assert "ENTERPRISE_EXPECTED_TABLES" not in client._child_env()

    def test_client_from_settings_passes_through(self) -> None:
        client = mod.client_from_settings(
            {
                "enterprise_mcp": {
                    "enabled": True,
                    "command": "python",
                    "args": [],
                    "cwd": ".",
                    "tool_timeout_sec": 5.0,
                }
            },
            expected_tables=("public.t",),
        )
        assert client is not None
        assert client._expected_tables == ("public.t",)


class TestGuardIsNotVacuous:
    def test_resolver_is_wired_into_the_client(self) -> None:
        source = ac.__file__ and open(ac.__file__, encoding="utf-8").read()
        assert "_expected_tables" in source, "резолвер отвалился от контекста"
        assert "expected_tables=" in source, "список не передаётся в клиент"

"""Контракт операций capability ``audit``.

Проверяется то, что важно вызывающей стороне, а не то, как устроена
библиотека (это уже сделано в ``test_audit_lib_*``):

* **ни одна операция не принимает SQL** — это вход, ради которого существует
  проверка по белому списку таблиц; он не должен появляться ни в одной
  сигнатуре и ни в одной опубликованной схеме;
* ``list_scripts`` отдаёт параметры **объектом** с типами и обязательностью,
  а не склеенной строкой имён (пункт 4.11);
* ``run_script`` **не возвращает SQL**, текст уходит в журнал (пункт 4.12);
* сломанный реестр даёт ``registry_unavailable``, а не пустой каталог, который
  выглядел бы как «скриптов нет» (пункт 4.13);
* внутренние коды библиотеки переведены в коды конверта (пункт 4.14).
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import pytest

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.errors import EnterpriseError
from libs.enterprise_common.registry import ToolDefinition, build_input_schema

from servers.enterprise.capabilities.audit.service.main import AuditService
from servers.enterprise.capabilities.audit.tools.generate_sql import (
    create_tool as create_generate_sql,
)
from servers.enterprise.capabilities.audit.tools.list_scripts import (
    create_tool as create_list_scripts,
)
from servers.enterprise.capabilities.audit.tools.run_script import (
    create_tool as create_run_script,
)

from test_audit_lib_fakes import (
    REGISTRY_TABLE,
    FakeSnapshot,
    make_registry_row,
    script_row_violations_by_period,
)

ALL_TABLES = ("oarb.audits", "oarb.violations", "oarb.audit_reports")


class FakeLlm:
    """Минимальный владелец HTTP: capability ``audit`` берёт его из контейнера."""

    def __init__(self, answer: str = "SELECT 1") -> None:
        self.answer = answer
        self.calls: list[list[dict[str, Any]]] = []

    def complete(self, *, messages: list[dict[str, Any]], audience: str = "") -> Any:
        self.calls.append(messages)
        return type("Completion", (), {"text": self.answer})()


def _container(snapshot: Any, *, tables: Any = ALL_TABLES, registry: str = REGISTRY_TABLE) -> ToolContainer:
    config = {
        "scripts_registry": {"table": registry},
        "audit": {"tables": list(tables or []), "row_ceiling": "1000"},
    }
    container = ToolContainer(services={"data": snapshot, "llm": FakeLlm()}, config=config)
    container.register("audit", AuditService(container=container, config=config))
    return container


def _snapshot(**kwargs: Any) -> FakeSnapshot:
    return FakeSnapshot(
        registry_rows=[script_row_violations_by_period()], **kwargs
    )


def _tool(name: str, container: ToolContainer) -> ToolDefinition:
    return {
        "list_scripts": create_list_scripts,
        "run_script": create_run_script,
        "generate_sql": create_generate_sql,
    }[name](container)


class TestNoSqlFromCaller:
    """Главный инвариант capability: SQL от вызывающей стороны не принимается."""

    @pytest.mark.parametrize("name", ["list_scripts", "run_script", "generate_sql"])
    def test_handler_has_no_sql_parameter(self, name: str) -> None:
        import inspect

        handler = _tool(name, _container(_snapshot())).handler
        assert "sql" not in inspect.signature(handler).parameters, (
            f"операция {name} принимает sql — это обходит проверку по белому списку"
        )

    @pytest.mark.parametrize("name", ["list_scripts", "run_script", "generate_sql"])
    def test_published_schema_has_no_sql_property(self, name: str) -> None:
        tool = _tool(name, _container(_snapshot()))
        assert tool.input_schema, f"у операции {name} пустая схема"
        properties = tool.input_schema.get("properties") or {}
        assert "sql" not in properties, f"в схеме {name} есть sql: {sorted(properties)}"

    @pytest.mark.parametrize("name", ["list_scripts", "run_script", "generate_sql"])
    def test_operation_is_not_model_facing(self, name: str) -> None:
        """Профиль вызова зашит в обработчик: у модели этих операций нет.

        Иначе модель получила бы вход, которого быть не должно, и путь
        «спросить пользователя и подставить SQL» стал бы доступен без
        всякой проверки.
        """
        tool = _tool(name, _container(_snapshot()))
        assert "runtime-only" in tool.tags, f"операция {name} не помечена runtime-only"
        assert tool.permissions, f"операция {name} не объявляет permission"


class TestListScripts:
    """4.11: параметры объектом, плюс подробное описание и что возвращает."""

    def test_parameters_are_objects_not_names(self) -> None:
        container = _container(_snapshot())
        payload = json.loads(_tool("list_scripts", container).handler())
        assert payload["count"] == 1
        script = payload["scripts"][0]
        assert script["name"] == "violations_by_period"
        names = [p["name"] for p in script["parameters"]]
        assert names == ["date_from", "date_to"]
        for param in script["parameters"]:
            assert set(param) >= {"name", "type", "required", "default", "description"}
            assert isinstance(param["type"], str)
            assert isinstance(param["required"], bool)
        assert "returns" in script
        assert "long_description" in script

    def test_validation_field_is_present_even_when_empty(self) -> None:
        """Поле ``validation`` обязано быть: его отсутствие читалось бы как
        «правил нет», а не как «правил не задано»."""
        container = _container(_snapshot())
        script = json.loads(_tool("list_scripts", container).handler())["scripts"][0]
        assert all("validation" in param for param in script["parameters"])

    def test_empty_registry_is_not_an_error(self) -> None:
        """Пустой реестр — законное состояние, и каталог пуст."""
        container = _container(FakeSnapshot(registry_rows=[]))
        payload = json.loads(_tool("list_scripts", container).handler())
        assert payload == {"count": 0, "scripts": []}

    def test_broken_registry_is_not_an_empty_catalog(self) -> None:
        """4.13: сломанный реестр — ``registry_unavailable``, не пустой список."""
        container = _container(FakeSnapshot(registry_error=RuntimeError("снимок недоступен")))
        with pytest.raises(EnterpriseError) as caught:
            _tool("list_scripts", container).handler()
        assert caught.value.code == "registry_unavailable", caught.value.code

    def test_registry_row_without_name_is_refused(self) -> None:
        """Молча пропустить такой скрипт нельзя: каталог выглядел бы полным."""
        container = _container(FakeSnapshot(registry_rows=[make_registry_row(name="")]))
        with pytest.raises(EnterpriseError) as caught:
            _tool("list_scripts", container).handler()
        assert caught.value.code == "registry_unavailable"


class TestRunScript:
    """4.12: SQL уходит в журнал, а не в ответ."""

    def test_response_has_no_sql(self) -> None:
        container = _container(_snapshot())
        payload = json.loads(
            _tool("run_script", container).handler(
                script="violations_by_period",
                params={"date_from": "2024-01-01", "date_to": "2024-12-31"},
            )
        )
        assert "sql" not in payload, "ответ содержит текст SQL — модель его перепишет"
        assert payload["status"] == "ok"
        assert payload["script_name"] == "violations_by_period"

    def test_sql_reaches_the_log(self) -> None:

        container = _container(_snapshot())
        with caplog_at_info() as records:
            _tool("run_script", container).handler(
                script="violations_by_period",
                params={"date_from": "2024-01-01", "date_to": "2024-12-31"},
            )
        joined = "\n".join(records)
        assert "oarb.violations" in joined, joined

    def test_unknown_script_is_not_found(self) -> None:
        container = _container(_snapshot())
        with pytest.raises(EnterpriseError) as caught:
            _tool("run_script", container).handler(script="нет_такого")
        assert caught.value.code == "not_found", caught.value.code

    def test_empty_script_name_is_invalid_params(self) -> None:
        container = _container(_snapshot())
        with pytest.raises(EnterpriseError) as caught:
            _tool("run_script", container).handler(script="   ")
        assert caught.value.code == "invalid_params", caught.value.code

    def test_missing_required_parameter_is_invalid_params(self) -> None:
        container = _container(_snapshot())
        with pytest.raises(EnterpriseError) as caught:
            _tool("run_script", container).handler(
                script="violations_by_period", params={"date_from": "2024-01-01"}
            )
        assert caught.value.code == "invalid_params", caught.value.code


class TestGenerateSql:
    def test_empty_question_is_invalid_params(self) -> None:
        container = _container(_snapshot())
        with pytest.raises(EnterpriseError) as caught:
            _tool("generate_sql", container).handler(query="")
        assert caught.value.code == "invalid_params", caught.value.code

    def test_empty_whitelist_is_explicit_refusal(self) -> None:
        """Пустой белый список запретил бы любой запрос, а выглядел бы как
        «индексов нет». Отказ должен называть причину."""
        container = _container(_snapshot(), tables=[])
        with pytest.raises(EnterpriseError) as caught:
            _tool("generate_sql", container).handler(query="сколько всего аудитов")
        assert caught.value.code == "registry_unavailable", caught.value.code
        assert "таблиц" in caught.value.message


class TestConfiguration:
    def test_missing_registry_table_is_explicit(self) -> None:
        container = _container(_snapshot(), registry="")
        with pytest.raises(EnterpriseError) as caught:
            _tool("list_scripts", container).handler()
        assert caught.value.code == "registry_unavailable"
        assert "ENTERPRISE_SCRIPTS_REGISTRY_TABLE" in caught.value.message

    def test_row_ceiling_must_be_integer(self) -> None:
        snapshot = _snapshot()
        config = {
            "scripts_registry": {"table": REGISTRY_TABLE},
            "audit": {"tables": list(ALL_TABLES), "row_ceiling": "много"},
        }
        container = ToolContainer(services={"data": snapshot, "llm": FakeLlm()}, config=config)
        with pytest.raises(EnterpriseError) as caught:
            AuditService(container=container, config=config)
        assert caught.value.code == "infrastructure_error", caught.value.code

    def test_missing_llm_service_is_infrastructure_error(self) -> None:
        """Сервис аудита без владельца модели — сбой сборки, а не KeyError."""
        config = {
            "scripts_registry": {"table": REGISTRY_TABLE},
            "audit": {"tables": list(ALL_TABLES), "row_ceiling": "1000"},
        }
        container = ToolContainer(services={"data": _snapshot()}, config=config)
        service = AuditService(container=container, config=config)
        with pytest.raises(EnterpriseError) as caught:
            service.generate_sql(query="сколько аудитов")
        assert caught.value.code == "infrastructure_error", caught.value.code
        assert "llm" in caught.value.message


class TestErrorTranslation:
    """4.14: один код на путь, а не «режим ошибок» по вызывающей стороне."""

    @pytest.mark.parametrize(
        ("internal", "envelope"),
        [
            ("not_found", "not_found"),
            ("validation_failed", "invalid_params"),
            ("registry_unavailable", "registry_unavailable"),
            ("registry_corrupt", "registry_unavailable"),
            ("forbidden_table", "forbidden_table"),
            ("row_limit_not_applied", "invalid_params"),
            ("generation_failed", "not_answerable"),
            ("guard_unavailable", "upstream_unavailable"),
            ("query_failed", "internal"),
            ("что-то_новое", "internal"),
        ],
    )
    def test_code_translation(self, internal: str, envelope: str) -> None:
        from libs.audit.errors import AuditError

        from servers.enterprise.capabilities.audit.service.main import _envelope

        assert _envelope(AuditError("текст", code=internal)).code == envelope

    def test_unknown_internal_code_does_not_leak(self) -> None:
        """Неизвестный код не должен доезжать до вызывающей стороны как есть:
        модель не знает, что с ним делать."""
        from libs.audit.errors import AuditError

        from servers.enterprise.capabilities.audit.service.main import _envelope

        assert _envelope(AuditError("текст", code="какой-то_новый_код")).code == "internal"


@contextlib.contextmanager
def caplog_at_info():
    """Захват INFO-журнала сервиса в тесте."""
    import logging

    class _Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.INFO)
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    capture = _Capture()
    logger = logging.getLogger("servers.enterprise.capabilities.audit.service.main")
    logger.addHandler(capture)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        yield capture.messages
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous)


def test_build_input_schema_is_not_empty() -> None:
    """Схема строится из сигнатуры: пустая схема означала бы, что модель не
    видит обязательные аргументы."""
    for factory in (create_list_scripts, create_run_script, create_generate_sql):
        tool: ToolDefinition = factory(_container(_snapshot()))
        assert tool.input_schema == build_input_schema(tool.handler)
        assert tool.category == "audit"

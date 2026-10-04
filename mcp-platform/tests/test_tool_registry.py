"""Реестр операций: валидация, схема аргументов, уникальность имён.

Стража, который ни разу не срабатывал, неотличим от стража, который ничего
не проверяет. Поэтому здесь на каждый пункт валидации есть тест на
заведомо плохих данных, а не только тест на хороших.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from libs.enterprise_common.errors import EnterpriseError  # noqa: E402
from libs.enterprise_common.registry import (  # noqa: E402
    ToolDefinition,
    ToolLoadError,
    ToolRegistry,
    _json_type,
    build_input_schema,
)


def _handler_ok() -> str:
    return "ok"


def _definition(**kwargs: object) -> ToolDefinition:
    base: dict[str, object] = {
        "name": "op",
        "description": "описание",
        "handler": _handler_ok,
        "category": "data",
    }
    base.update(kwargs)
    return ToolDefinition(**base)  # type: ignore[arg-type]


class TestSchemaBuilding:
    def test_primitives_are_mapped(self) -> None:
        def handler(name: str, count: int, ratio: float, flag: bool) -> None:
            return None

        schema = build_input_schema(handler)
        assert schema["properties"]["name"]["type"] == "string"
        assert schema["properties"]["count"]["type"] == "integer"
        assert schema["properties"]["ratio"]["type"] == "number"
        assert schema["properties"]["flag"]["type"] == "boolean"
        assert schema["required"] == ["name", "count", "ratio", "flag"]

    def test_optional_parameter_is_not_required(self) -> None:
        def handler(name: str, note: str = "") -> None:
            return None

        schema = build_input_schema(handler)
        assert schema["required"] == ["name"]

    def test_containers_are_mapped(self) -> None:
        def handler(items: list[str], payload: dict[str, object]) -> None:
            return None

        schema = build_input_schema(handler)
        assert schema["properties"]["items"]["type"] == "array"
        assert schema["properties"]["payload"]["type"] == "object"

    def test_optional_union_is_unwrapped(self) -> None:
        """Регрессия: у ``int | None`` есть ``__name__ == "Union"``.

        Подстрочная проверка объявляла такое объединение строкой, и модель
        получала подсказку «передай число строкой». При этом ``NoneType``
        больше не выбрасывается молча: объединение публикуется списком типов
        вместе с ``null``, иначе значение, которое обработчик принимает,
        отвергалось бы валидатором SDK ДО конвейера.
        """

        def handler(count: int | None = None, payload: dict[str, object] | None = None) -> None:
            return None

        schema = build_input_schema(handler)
        assert schema["properties"]["count"]["type"] == ["integer", "null"]
        assert schema["properties"]["payload"]["type"] == ["object", "null"]
        assert schema["required"] == []

    def test_optional_container_union_accepts_null(self) -> None:
        """Ровно та форма, которая роняла ``claim_task`` на каждом опросе.

        ``list[str] | None = None`` публиковался как ``{"type": "array"}``, и
        ``null`` — обычный поллинг — отвергался. Теперь ``null`` в списке типов.
        """

        def handler(priority_contents: list[str] | None = None) -> None:
            return None

        schema = build_input_schema(handler)
        assert schema["properties"]["priority_contents"]["type"] == ["array", "null"]
        assert schema["required"] == []

    def test_non_nullable_union_keeps_single_type(self) -> None:
        """Объединение без ``NoneType``, но однородное по JSON-типу.

        Две аннотации, дающие один и тот же тип JSON, — это не неоднородное
        объединение: схема у него есть, и она точна.
        """

        def handler(items: list[str] | set[str]) -> None:
            return None

        schema = build_input_schema(handler)
        assert schema["properties"]["items"]["type"] == "array"
        assert schema["required"] == ["items"]

    def test_heterogeneous_union_is_refused(self) -> None:
        """Неоднородное объединение объявлять нечем — значит, объявлять не будем.

        Раньше бралась первая часть, и модель получала заведомо неверный тип.
        Отказ на загрузке виден сразу; отказ на проводе — нет.
        """

        def handler(count: int | str) -> None:
            return None

        with pytest.raises(ToolLoadError) as excinfo:
            build_input_schema(handler)
        message = str(excinfo.value)
        assert "count" in message, message
        assert "handler" in message, message

    def test_json_type_rejects_nothing_harmfully(self) -> None:
        assert _json_type(object) == "object"
        assert _json_type(int) == "integer"
        assert _json_type(str) == "string"
        assert _json_type(int | None) == ["integer", "null"]
        assert _json_type(type(None)) == "null"

    def test_missing_annotation_fails(self) -> None:
        def handler(name) -> None:  # noqa: ANN001 - намеренно без аннотации
            return None

        with pytest.raises(ToolLoadError, match="без аннотации типа"):
            build_input_schema(handler)

    def test_var_args_rejected(self) -> None:
        def handler(*args: str) -> None:
            return None

        with pytest.raises(ToolLoadError, match=r"\*args/\*\*kwargs"):
            build_input_schema(handler)


class TestRegistryValidation:
    """Каждый пункт 2.3 — отдельный тест на плохих данных."""

    def test_valid_definition_registers(self) -> None:
        registry = ToolRegistry()
        registry.register(_definition())
        assert "op" in registry
        assert len(registry) == 1

    def test_wrong_type_rejected(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(ToolLoadError, match="ожидался ToolDefinition"):
            registry.register({"name": "op"})  # type: ignore[arg-type]

    def test_empty_name_rejected(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(ToolLoadError, match="имя операции не должно быть пустым"):
            registry.register(_definition(name="   "))

    def test_empty_description_rejected(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(ToolLoadError, match="описание операции не должно быть пустым"):
            registry.register(_definition(description=" "))

    def test_empty_category_rejected(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(ToolLoadError, match="категория"):
            registry.register(_definition(category=""))

    def test_non_callable_handler_rejected(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(ToolLoadError, match="handler обязан быть вызываемым"):
            registry.register(_definition(handler="not a function"))  # type: ignore[arg-type]

    def test_duplicate_name_rejected(self) -> None:
        registry = ToolRegistry()
        registry.register(_definition())
        with pytest.raises(ToolLoadError, match="уже зарегистрировано"):
            registry.register(_definition())

    def test_duplicate_across_capabilities_rejected(self) -> None:
        """Имена уникальны глобально: агент видит плоский список инструментов."""
        registry = ToolRegistry()
        registry.register(_definition(category="data"))
        with pytest.raises(ToolLoadError, match="уже зарегистрировано"):
            registry.register(_definition(category="audit"))

    def test_unknown_name_lookup_raises(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(ToolLoadError, match="не зарегистрирована"):
            registry.get("missing")

    def test_tool_load_error_is_enterprise_error(self) -> None:
        """Наружу наружу уходит доменная ошибка, а не AttributeError."""
        assert issubclass(ToolLoadError, EnterpriseError)

    def test_disabled_definition_hidden_from_iteration(self) -> None:
        registry = ToolRegistry()
        registry.register(_definition(name="on", enabled=True))
        registry.register(_definition(name="off", enabled=False))
        assert registry.names() == ("off", "on")
        assert [d.name for d in registry] == ["on"]
        assert len(registry) == 2

    def test_by_category_groups(self) -> None:
        registry = ToolRegistry()
        registry.register(_definition(name="a", category="data"))
        registry.register(_definition(name="b", category="audit"))
        grouped = registry.by_category()
        assert set(grouped) == {"audit", "data"}
        assert [d.name for d in grouped["data"]] == ["a"]


class TestToolLoadErrorMessage:
    def test_message_carries_path_and_name(self) -> None:
        """По одному сообщению невозможно понять, какой из сорока файлов сломан."""
        error = ToolLoadError("сломалось", path="capabilities/data/tools/x.py", name="op")
        text = str(error)
        assert "capabilities/data/tools/x.py" in text
        assert "op" in text
        assert "сломалось" in text

"""Реестр операций: единственный источник правды о том, что умеет сервер.

Зачем реестр, если у ``FastMCP`` есть свой:

* ``FastMCP`` хранит операции в плоском словаре и узнаёт о них только в момент
  регистрации. Список «что вообще есть» приходится собирать отдельно, обходом
  исходников.
* Capability приходят файлами. Реестр позволяет **валидировать** их до старта:
  опечатка в имени или несовместимая сигнатура handler'а должны валить сборку,
  а не всплывать при первом обращении из production.
* Один реестр обслуживает две разные поверхности: модель видит подмножество
  операций, а рантайм агента (очередь задач, журнал) — ту же структуру напрямую.
  Делить их на уровне реестра нельзя: иначе появятся две копии описания.

Реестр транспортно-независим: здесь нет ни ``FastMCP``, ни ``psycopg2``.
"""

from __future__ import annotations

import inspect
import types
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Union, get_args, get_origin, get_type_hints

from libs.enterprise_common.errors import EnterpriseError


class ToolLoadError(EnterpriseError):
    """Ошибка загрузки операции.

    Обязана нести **путь до файла** и **имя операции**: по одному сообщению
    ``AttributeError`` невозможно понять, какой из сорока файлов сломан.
    """

    code = "tool_load_error"

    def __init__(self, message: str, *, path: str = "", name: str = "") -> None:
        super().__init__(message)
        self.path = path
        self.name = name

    def __str__(self) -> str:
        where = self.path or "<unknown path>"
        what = f" [{self.name}]" if self.name else ""
        return f"{where}{what}: {self.message}"


@dataclass(frozen=True)
class ToolDefinition:
    """Описание одной операции.

    ``handler`` — функция с аннотированными параметрами. Из неё строится
    ``input_schema``, который агент видит в discovery; отдельного JSON-файла
    со схемой нет намеренно: две записи об одном параметре разъезжаются, и
    расхождение обнаруживается только в рантайме.
    """

    name: str
    description: str
    handler: Callable[..., Any]
    category: str
    version: str = "1"
    enabled: bool = True
    tags: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    input_schema: Mapping[str, Any] = field(default_factory=dict)

    def to_summary(self) -> dict[str, Any]:
        """Урезанное описание для логов и health-отчёта."""
        return {
            "name": self.name,
            "category": self.category,
            "version": self.version,
            "enabled": self.enabled,
            "tags": list(self.tags),
        }


def build_input_schema(handler: Callable[..., Any]) -> dict[str, Any]:
    """Собрать JSON-схему параметров по сигнатуре обработчика.

    Требование — аннотация каждого параметра. Без неё MCP не знает тип и
    показывает агенту ``any``, и ошибка всплывает как трассировка внутри
    инструмента вместо понятного отказа до запуска.
    """
    signature = inspect.signature(handler)
    try:
        hints = get_type_hints(handler)
    except Exception as exc:  # noqa: BLE001 - неразрешимые аннотации
        raise ToolLoadError(
            f"не удалось разрешить аннотации обработчика: {exc}",
            name=getattr(handler, "__name__", ""),
        ) from exc

    properties: dict[str, Any] = {}
    required: list[str] = []
    for param_name, param in signature.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise ToolLoadError(
                f"обработчик принимает *args/**kwargs ({param_name}); "
                "схему аргументов по ним построить нельзя",
                name=getattr(handler, "__name__", ""),
            )
        if param_name in ("self", "cls"):
            continue
        if param_name not in hints:
            raise ToolLoadError(
                f"параметр {param_name!r} без аннотации типа",
                name=getattr(handler, "__name__", ""),
            )
        annotation = hints[param_name]
        properties[param_name] = {"type": _json_type(annotation)}
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _json_type(annotation: Any) -> str:
    """Отображение аннотации Python → JSON Schema.

    Разбирается через ``get_origin``/``get_args``, а не через подстроку в
    ``str(annotation)``: у ``int | None`` есть ``__name__ == "Union"``, и
    подстрочная проверка молча объявляла любую аннотацию-объединение строкой.
    Для модели это означало бы «передай словарь строкой».
    """
    origin = get_origin(annotation)

    if origin is Union or origin is types.UnionType:
        parts = [a for a in get_args(annotation) if a is not type(None)]
        if not parts:
            return "string"
        if len(parts) > 1:
            # Объединение неоднородно (например int | str). Точной схемы у
            # него нет; берём первую непустую часть и не врём дальше.
            return _json_type(parts[0])
        return _json_type(parts[0])

    if origin in (list, set, frozenset, tuple):
        return "array"
    if origin is dict:
        return "object"
    if annotation is Any or annotation is object:
        # Любое значение. ``string`` здесь вводил бы в заблуждение сильнее,
        # чем ``object``: модель начала бы присылать всё строкой.
        return "object"
    if annotation is type(None):
        return "null"
    if isinstance(annotation, type):
        if issubclass(annotation, bool):
            return "boolean"
        if issubclass(annotation, int):
            return "integer"
        if issubclass(annotation, float):
            return "number"
        if issubclass(annotation, str):
            return "string"
    return "string"


class ToolRegistry:
    """Реестр операций с проверкой уникальности имён.

    Имена уникальны глобально, а не внутри capability: агент видит плоский
    список инструментов, и две операции с одинаковым именем в разных
    capability означают, что одна из них молча вытесняет другую.
    """

    def __init__(self, definitions: Iterable[ToolDefinition] = ()) -> None:
        self._by_name: dict[str, ToolDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ToolDefinition) -> None:
        if not isinstance(definition, ToolDefinition):
            raise ToolLoadError(
                f"ожидался ToolDefinition, получен {type(definition).__name__}",
                name=getattr(definition, "name", "") or "",
            )
        if not definition.name.strip():
            raise ToolLoadError("имя операции не должно быть пустым")
        if not definition.description.strip():
            raise ToolLoadError("описание операции не должно быть пустым", name=definition.name)
        if not callable(definition.handler):
            raise ToolLoadError("handler обязан быть вызываемым", name=definition.name)
        if not definition.category.strip():
            raise ToolLoadError("категория (capability) не должна быть пустой", name=definition.name)
        if definition.name in self._by_name:
            raise ToolLoadError("имя уже зарегистрировано", name=definition.name)
        self._by_name[definition.name] = definition

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise ToolLoadError("операция не зарегистрирована", name=name) from exc

    def enabled(self) -> tuple[ToolDefinition, ...]:
        return tuple(d for d in self._by_name.values() if d.enabled)

    def by_category(self) -> dict[str, tuple[ToolDefinition, ...]]:
        grouped: dict[str, list[ToolDefinition]] = {}
        for definition in self._by_name.values():
            grouped.setdefault(definition.category, []).append(definition)
        return {k: tuple(v) for k, v in sorted(grouped.items())}

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    def summaries(self) -> list[dict[str, Any]]:
        return [d.to_summary() for d in self.enabled()]

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __iter__(self) -> Iterator[ToolDefinition]:
        return iter(self.enabled())

    def __len__(self) -> int:
        return len(self._by_name)

    def __repr__(self) -> str:
        return f"ToolRegistry({len(self)} ops: {', '.join(self.names()[:6])})"

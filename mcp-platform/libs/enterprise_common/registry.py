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
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Union, get_args, get_origin, get_type_hints

from libs.enterprise_common.errors import EnterpriseError
from libs.enterprise_common.execution.quality import POLICY_NAMES


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

    ``input_schema``, если он задан, — **та схема, что уходит на провод**, и
    вывод из подписи тогда не выполняется. Объявление нужно там, где подпись
    выводима недостаточно (``items``, ``description``, состав объектов) или где
    автоматический вывод опасен. Пустой дефолт — единственный признак
    «не объявлено», отдельного поля для этого нет.

    ``category`` — **имя capability**, а не свободная классификация. По нему
    выбираются политика (``execution/policy.py``), журнал и артефакты вызова
    (``execution/pipeline.py``), и им же ``by_category`` группирует реестр.
    Поле обязательно, и автор объявляет принадлежность к capability дважды:
    каталогом, в который он положил файл, и этой строкой. Проверяются обе
    половины и по отдельности — пустое значение отвергает
    :meth:`ToolRegistry.register`, а расхождение с каталогом
    ``loader.load_definition``. Отказы разные, потому что и правки разные:
    пустой категории не хватает значения, чужую надо переименовать.
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
    quality_policy: str = "default"

    def wants_context(self) -> bool:
        """Заявляет ли обработчик служебный параметр ``context``."""
        return CONTEXT_PARAM in inspect.signature(self.handler).parameters

    def to_summary(self) -> dict[str, Any]:
        """Урезанное описание для логов и health-отчёта."""
        return {
            "name": self.name,
            "category": self.category,
            "version": self.version,
            "enabled": self.enabled,
            "tags": list(self.tags),
            "quality_policy": self.quality_policy,
        }


#: Служебный параметр контекста. Объявлен в слое исполнения и импортируется
#: оттуда: имя-параметр обработчика принадлежит контракту вызова, а не реестру.
from libs.enterprise_common.execution.context import CONTEXT_PARAM  # noqa: E402

#: Параметры идентичности. Их появление в сигнатуре — нарушение контракта
#: вызова: идентичность приходит в ``params._meta`` (§ ``runtime/call-contract``)
#: и в опубликованной схеме ей не места.
IDENTITY_PARAMS: frozenset[str] = frozenset({"session_id", "user_id", "request_id"})


def validate_handler(
    handler: Callable[..., Any], *, name: str = "", path: str = ""
) -> None:
    """Проверить форму обработчика операции.

    Проверка на загрузке, а не на первом вызове: поднятый сервер с операцией,
    которая упала бы на первой же сессии, выглядит рабочим ровно до первого
    обращения к ней.
    """
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError) as exc:
        raise ToolLoadError(
            f"не удалось прочитать сигнатуру обработчика: {exc}", path=path, name=name
        ) from exc

    offending = sorted(IDENTITY_PARAMS.intersection(signature.parameters))
    if offending:
        raise ToolLoadError(
            f"обработчик объявляет параметр(ы) идентичности {', '.join(offending)}; "
            "идентичность приходит в params._meta и доставляется через параметр "
            f"'{CONTEXT_PARAM}' (ToolExecutionContext)",
            path=path,
            name=name,
        )

    if CONTEXT_PARAM not in signature.parameters:
        return
    positional = [
        p
        for p in list(signature.parameters.values())[:1]
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if not positional or positional[0].name != CONTEXT_PARAM:
        raise ToolLoadError(
            f"параметр {CONTEXT_PARAM!r} обязан быть первым: конвейер подставляет "
            "контекст до доменных аргументов",
            path=path,
            name=name,
        )
    try:
        hints = get_type_hints(handler)
    except Exception as exc:  # noqa: BLE001 - неразрешимые аннотации
        raise ToolLoadError(
            f"не удалось разрешить аннотации обработчика: {exc}", path=path, name=name
        ) from exc
    annotation = hints.get(CONTEXT_PARAM)
    if annotation is None:
        raise ToolLoadError(
            f"параметр {CONTEXT_PARAM!r} без аннотации типа", path=path, name=name
        )
    # Строковое сравнение: `from __future__ import annotations` оставляет в
    # подсказках строки, и `get_type_hints` их разрешает, но аннотация может
    # прийти и как строка из замыкания — тогда единственное честное сравнение
    # это по имени класса.
    actual = annotation if isinstance(annotation, type) else type(annotation)
    if getattr(actual, "__name__", "") != "ToolExecutionContext":
        raise ToolLoadError(
            f"параметр {CONTEXT_PARAM!r} должен быть аннотирован ToolExecutionContext, "
            f"а не {getattr(actual, '__name__', annotation)!r}",
            path=path,
            name=name,
        )


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
        if param_name == CONTEXT_PARAM:
            # Служебный параметр конвейера. В схему он не попадает: модель его
            # не заполняет и не должна видеть — всё назначение `context` в том,
            # что подставляет его сервер.
            continue
        if param_name not in hints:
            raise ToolLoadError(
                f"параметр {param_name!r} без аннотации типа",
                name=getattr(handler, "__name__", ""),
            )
        annotation = hints[param_name]
        properties[param_name] = {
            "type": _json_type(
                annotation,
                owner=getattr(handler, "__name__", ""),
                param=param_name,
            )
        }
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _json_type(annotation: Any, *, owner: str = "", param: str = "") -> str | list[str]:
    """Отображение аннотации Python → JSON Schema.

    Разбирается через ``get_origin``/``get_args``, а не через подстроку в
    ``str(annotation)``: у ``int | None`` есть ``__name__ == "Union"``, и
    подстрочная проверка молча объявляла любую аннотацию-объединение строкой.
    Для модели это означало бы «передай словарь строкой».

    ``NoneType`` в объединении **не выбрасывается**. Раньше он отбрасывался, и
    ``list[str] | None`` публиковался как ``{"type": "array"}``: значение
    ``null`` — законное, штатное, означающее «без фильтра», — отвергалось
    валидатором SDK как ``None is not of type 'array'``. Отказ рождался ДО
    конвейера, поэтому не классифицировался платформой и не попадал в журнал:
    агент видел отказ, журнал молчал. Теперь такое объединение публикуется
    списком типов ``["array", "null"]`` — идиома уже принята в проекте
    (``log_events.py``).

    Неоднородное объединение без ``NoneType`` (``int | str``) точной схемы не
    имеет: брать первую часть — значит объявить заведомо неверный тип, и
    модель пришлёт не то. Такая операция обязана объявить схему руками, иначе
    загрузка падает здесь, а не отказом на проводе.
    """
    origin = get_origin(annotation)

    if origin is Union or origin is types.UnionType:
        args = get_args(annotation)
        nullable = type(None) in args
        parts = [a for a in args if a is not type(None)]
        if not parts:
            return "null"
        types_found: list[str] = []
        for part in parts:
            found = _json_type(part, owner=owner, param=param)
            for name in found if isinstance(found, list) else [found]:
                if name not in types_found:
                    types_found.append(name)
        if len(types_found) > 1:
            # Неоднородное объединение. Единственное, что можно объявить
            # честно, — «любой из типов», но модель получит подсказку, которой
            # нет у обработчика, и отказ станет ещё и её виной. Отказываем.
            raise ToolLoadError(
                f"параметр {param or '<без имени>'!r} аннотирован неоднородным "
                f"объединением ({types_found}); схема для него не выводится — "
                "объявите input_schema руками",
                name=owner,
            )
        if not nullable:
            return types_found[0]
        # Тип один и nullable — публикуем список, а не одиночный тип.
        return [types_found[0], "null"]

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
        if definition.quality_policy not in POLICY_NAMES:
            raise ToolLoadError(
                f"неизвестная политика качества {definition.quality_policy!r}; "
                f"допустимы: {', '.join(sorted(POLICY_NAMES))}",
                name=definition.name,
            )
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

"""Загрузчик операций: обход capability-каталогов и валидация при старте.

Контракт одного файла операции::

    # capabilities/data/tools/log_event.py
    from libs.enterprise_common.container import ToolContainer
    from libs.enterprise_common.registry import ToolDefinition

    def create_tool(container: ToolContainer) -> ToolDefinition:
        return ToolDefinition(
            name="log_event",
            description="...",
            handler=handle_log_event,
            category="data",
        )

Добавление файла не должно требовать правок в ``server.py`` — иначе через
месяц ни одна новая операция не добавится.

**Fail-fast — обязателен.** Ошибка одного файла валит старт сервера целиком.
Частично загруженный сервер хуже не загруженного: он принимает соединение,
а половина операций отсутствует, и это обнаруживается в проде на конкретном
вызове. Сообщение обязано называть путь до файла и имя операции — иначе в
сорока файлах непонятно, какой сломан.
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition, ToolLoadError, ToolRegistry, build_input_schema

logger = logging.getLogger(__name__)

#: Имя точки входа в файле операции. Фиксировано, чтобы поиск не превратился
#: в соглашение: ровно одно имя, ровно одно место.
ENTRY_POINT = "create_tool"


def discover_tool_files(capabilities_dir: Path) -> list[Path]:
    """Рекурсивно найти файлы операций: ``capabilities/*/tools/*.py``.

    Порядок стабильный: ``sorted``. Недетерминированный порядок даёт
    непредсказуемый порядок регистрации, а с ним — разный порядок в discovery
    и разные тексты логов при одном и том же коде.
    """
    if not capabilities_dir.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(capabilities_dir.glob("*/tools/**/*.py")):
        if path.name == "__init__.py" or path.name.startswith("_"):
            continue
        found.append(path)
    return found


def _module_name(path: Path, root: Path) -> str:
    """Понятное модульное имя вида ``servers.enterprise.capabilities.data.tools.x``.

    Имя выводится из пути, а не выдумывается: тогда ``importlib`` не создаёт
    дубликаты модуля при повторной загрузке в том же процессе.
    """
    relative = path.relative_to(root).with_suffix("")
    return ".".join(relative.parts)


def _import_module(path: Path, root: Path) -> ModuleType:
    name = _module_name(path, root)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ToolLoadError("не удалось построить спецификацию модуля", path=str(path))
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - причина вложена в сообщение
        raise ToolLoadError(f"модуль не импортируется: {exc!r}", path=str(path)) from exc
    return module


def load_definition(path: Path, container: ToolContainer, root: Path) -> ToolDefinition:
    """Загрузить и провалидировать одну операцию. Бросает ``ToolLoadError``."""
    module = _import_module(path, root)

    entry = getattr(module, ENTRY_POINT, None)
    if entry is None:
        raise ToolLoadError(
            f"в модуле нет точки входа {ENTRY_POINT}(container)", path=str(path)
        )
    if not callable(entry):
        raise ToolLoadError(f"{ENTRY_POINT} обязан быть вызываемым", path=str(path))

    try:
        definition = entry(container)
    except Exception as exc:  # noqa: BLE001
        raise ToolLoadError(f"{ENTRY_POINT} упал: {exc!r}", path=str(path)) from exc

    if not isinstance(definition, ToolDefinition):
        raise ToolLoadError(
            f"{ENTRY_POINT} обязан вернуть ToolDefinition, вернул {type(definition).__name__}",
            path=str(path),
        )

    name = definition.name
    if not name.strip():
        raise ToolLoadError("имя операции не должно быть пустым", path=str(path), name=name)
    if not definition.description.strip():
        raise ToolLoadError("описание операции не должно быть пустым", path=str(path), name=name)
    if not callable(definition.handler):
        raise ToolLoadError("handler обязан быть вызываемым", path=str(path), name=name)

    # Схема строится здесь, а не автором файла: иначе описание параметра и его
    # сигнатура разъезжаются, и расхождение всплывает в рантайме.
    try:
        schema = build_input_schema(definition.handler)
    except ToolLoadError as exc:
        raise ToolLoadError(exc.message, path=str(path), name=name) from exc

    return ToolDefinition(
        name=definition.name,
        description=definition.description,
        handler=definition.handler,
        category=definition.category,
        version=definition.version,
        enabled=definition.enabled,
        tags=definition.tags,
        permissions=definition.permissions,
        input_schema=schema,
    )


def load_registry(
    capabilities_dir: Path,
    container: ToolContainer,
    root: Path | None = None,
) -> ToolRegistry:
    """Собрать реестр из всех capability-каталогов.

    Бросает ``ToolLoadError`` на первой проблеме — см. модульный докстринг.
    """
    platform_root = root if root is not None else capabilities_dir.parent.parent
    registry = ToolRegistry()
    for path in discover_tool_files(capabilities_dir):
        definition = load_definition(path, container, platform_root)
        try:
            registry.register(definition)
        except ToolLoadError as exc:
            raise ToolLoadError(
                exc.message,
                path=str(path),
                name=exc.name or definition.name,
            ) from exc
        logger.debug("операция загружена: %s", definition.name)
    return registry


def register_with_server(registry: ToolRegistry, mcp: Any) -> list[ToolDefinition]:
    """Зарегистрировать операции реестра в ``FastMCP`` и вернуть их.

    Единственное место, где платформа знает про MCP. Сервисы и реестр о нём
    не знают — иначе тест сервиса без MCP перестаёт быть возможным.
    """
    registered: list[ToolDefinition] = []
    for definition in registry:
        mcp.add_tool(
            _wrap(definition),
            name=definition.name,
            description=definition.description,
        )
        registered.append(definition)
    return registered


def _wrap(definition: ToolDefinition) -> Any:
    """Подогнать обработчик под ожидаемую MCP сигнатуру.

    Доменная ошибка наружу уходит как ``ValueError`` с её кодом: агент не
    должен видеть ни traceback, ни исключения драйверов.
    """
    from libs.enterprise_common.errors import EnterpriseError

    def call(**kwargs: Any) -> Any:
        try:
            return definition.handler(**kwargs)
        except EnterpriseError as exc:
            raise ValueError(f"[{exc.code}] {exc.message}") from exc

    call.__name__ = definition.name
    call.__doc__ = definition.description
    return call


def iter_definitions(registry: ToolRegistry) -> Iterable[ToolDefinition]:
    return iter(registry)

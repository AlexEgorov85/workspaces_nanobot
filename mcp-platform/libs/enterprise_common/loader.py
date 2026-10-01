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
import json
import logging
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.errors import EnterpriseError
from libs.enterprise_common.registry import ToolDefinition, ToolLoadError, ToolRegistry, build_input_schema

logger = logging.getLogger(__name__)

#: Имя точки входа в файле операции. Фиксировано, чтобы поиск не превратился
#: в соглашение: ровно одно имя, ровно одно место.
ENTRY_POINT = "create_tool"


def discover_tool_files(
    capabilities_dir: Path, capabilities: Iterable[str] | None = None
) -> list[Path]:
    """Рекурсивно найти файлы операций: ``capabilities/*/tools/*.py``.

    Args:
        capabilities: оставить только эти capability. ``None`` — все.

    Порядок стабильный: ``sorted``. Недетерминированный порядок даёт
    непредсказуемый порядок регистрации, а с ним — разный порядок в discovery
    и разные тексты логов при одном и том же коде.
    """
    if not capabilities_dir.is_dir():
        return []
    wanted = set(capabilities) if capabilities is not None else None
    found: list[Path] = []
    for path in sorted(capabilities_dir.glob("*/tools/**/*.py")):
        if path.name == "__init__.py" or path.name.startswith("_"):
            continue
        if wanted is not None and path.relative_to(capabilities_dir).parts[0] not in wanted:
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
    capabilities: Iterable[str] | None = None,
) -> ToolRegistry:
    """Собрать реестр из capability-каталогов.

    Args:
        capabilities: собрать только эти capability. ``None`` — все.

    Бросает ``ToolLoadError`` на первой проблеме — см. модульный докстринг.
    """
    platform_root = root if root is not None else capabilities_dir.parent.parent
    registry = ToolRegistry()
    for path in discover_tool_files(capabilities_dir, capabilities):
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


def build_server(
    registry: ToolRegistry,
    *,
    name: str,
    version: str = "1",
    instructions: str | None = None,
) -> Any:
    """Собрать MCP-сервер из реестра на базовом API пакета ``mcp``.

    Осознанно **не** ``FastMCP``: удобная обёртка выводит схему параметров
    своей разведкой сигнатуры, и на проводе оказывается вторая схема, а
    расхождение с той, что валидируется при загрузке, обнаруживается только
    в рантайме. Здесь на провод уходит ровно ``ToolDefinition.input_schema``.

    Единственное место в платформе, где встречается протокол MCP: реестр,
    сервисы и определения о нём не знают, иначе тест сервиса без протокола
    перестал бы быть возможным.
    """
    import anyio
    from mcp.server.lowlevel import Server
    from mcp.types import CallToolResult, TextContent, Tool

    server = Server(name, version=version, instructions=instructions)

    @server.list_tools()
    async def list_tools() -> list[Any]:
        return [
            Tool(
                name=definition.name,
                description=definition.description,
                inputSchema=dict(definition.input_schema),
            )
            for definition in registry
        ]

    @server.call_tool()
    async def call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
        def _error(code: str, message: str) -> Any:
            return CallToolResult(
                content=[TextContent(type="text", text=f"[{code}] {message}")],
                isError=True,
            )

        try:
            definition = registry.get(tool_name)
        except EnterpriseError as exc:
            return _error(exc.code, exc.message)

        # Обработчики синхронные и ходят в пул PostgreSQL. Вызов из event loop
        # без разгрузки заблокировал бы весь сервер, включая отмену хода.
        def invoke() -> Any:
            return definition.handler(**arguments)

        try:
            result = await anyio.to_thread.run_sync(invoke)
        except EnterpriseError as exc:
            # Доменная ошибка: агент получает код и текст, а не traceback.
            return _error(exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001
            return _error("internal_error", f"{type(exc).__name__}: {exc}")

        return CallToolResult(
            content=[TextContent(type="text", text=_as_text(result))],
            isError=False,
        )

    return server


def _as_text(result: Any) -> str:
    """Привести результат операции к тексту ответа.

    Операции возвращают JSON-строку сами: формат ответа — часть контракта
    операции, а не решение транспорта.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False)
    return str(result)


def iter_definitions(registry: ToolRegistry) -> Iterable[ToolDefinition]:
    return iter(registry)

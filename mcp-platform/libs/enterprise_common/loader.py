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
from libs.enterprise_common.errors import EnterpriseError
from libs.enterprise_common.execution.quality import POLICY_NAMES
from libs.enterprise_common.registry import (
    ToolDefinition,
    ToolLoadError,
    ToolRegistry,
    build_input_schema,
    validate_handler,
)

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

    # Форма обработчика проверяется до схемы: объявление параметра идентичности —
    # нарушение контракта вызова, и сообщение о нём должно называть параметр, а
    # не приходить обёрнутым в «ошибка построения схемы».
    try:
        validate_handler(definition.handler, name=name, path=str(path))
    except ToolLoadError as exc:
        raise ToolLoadError(exc.message, path=str(path), name=name) from exc

    # Объявленная автором схема — это схема. Раньше она затиралась здесь
    # безусловно, и объявление ``INPUT_SCHEMA`` в файлах операций было мёртвой
    # декларацией, выглядящей авторитетно: автор читал её, чтобы понять контракт
    # операции, и она не говорила правды. Из-за этого потерялась правка
    # ``claim_task`` (``"type": ["array","null"]``) — верная по смыслу и
    # неиспользуемая.
    #
    # Состояние «объявлено, а на проводе другое» недопустимо: оно ловушка и
    # стоило уже одной потерянной правки. Промежуточного варианта нет — либо
    # объявление истинно, либо его нет. «Не объявлено» выражается существующим
    # дефолтом ``ToolDefinition.input_schema = {}``; отдельного поля не заводим.
    try:
        schema = (
            dict(definition.input_schema)
            if definition.input_schema
            else build_input_schema(definition.handler)
        )
    except ToolLoadError as exc:
        raise ToolLoadError(exc.message, path=str(path), name=name) from exc

    if definition.quality_policy not in POLICY_NAMES:
        raise ToolLoadError(
            f"неизвестная политика качества {definition.quality_policy!r}; "
            f"допустимы: {', '.join(sorted(POLICY_NAMES))}",
            path=str(path),
            name=name,
        )

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
        quality_policy=definition.quality_policy,
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
    pipeline: Any,
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

    Метаданные вызова читаются **здесь и один раз**: обработчик операции
    получает ``ToolExecutionContext`` от конвейера, а разбирать ``params._meta``
    в шестнадцати файлах операций означало бы шестнадцать мест, где это можно
    сделать по-разному.
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
            _log_call(tool_name, "error", code=exc.code)
            return _error(exc.code, exc.message)

        # ``server.request_context.meta`` — разобранные метаданные запроса,
        # которые mcp положил в RequestContext. Значение может быть None:
        # вызов без `_meta` законен на проводе и обязан быть отклонён конвейером
        # с `identity_missing`, а не разыгран как пустой словарь.
        meta = _request_meta(server)

        # Обработчики синхронные и ходят в пул PostgreSQL. Вызов из event loop
        # без разгрузки заблокировал бы весь сервер, включая отмену хода.
        def invoke() -> Any:
            return pipeline.execute(definition, arguments, meta)

        try:
            outcome = await anyio.to_thread.run_sync(invoke)
        except Exception as exc:  # noqa: BLE001 - конвейер не должен ронять сервер
            # Сбой конвейера — единственный исход, у которого нет ни исхода, ни
            # длительности от ``PipelineResult``: их неоткуда взять, и выдуманное
            # значение выглядело бы как замер. Поэтому ``duration_ms`` здесь
            # отсутствует, а не равно нулю.
            _log_call(tool_name, "error", code="internal_error")
            return _error("internal_error", f"{type(exc).__name__}: {exc}")

        _log_call(
            tool_name,
            outcome.status,
            code="" if not outcome.is_error else outcome.code,
            duration_ms=outcome.duration_ms,
        )
        return CallToolResult(
            content=[TextContent(type="text", text=outcome.text)],
            isError=bool(outcome.is_error),
        )

    return server


def _log_call(
    tool_name: str,
    status: str,
    *,
    code: str = "",
    duration_ms: int | None = None,
) -> None:
    """Одна строка stderr на вызов: имя операции, исход, длительность.

    Зачем она, когда библиотека ``mcp`` уже пишет на каждый запрос
    ``Processing request of type CallToolRequest``: та строка называет **тип
    запроса протокола**, а не операцию, и не называет ни исхода, ни времени.
    По ней нельзя ответить на вопрос «что вызывалось и чем кончилось», а
    вызывающий в это время получил ``isError`` и ушёл. Отказ, не оставивший
    следа в stderr, — тот же класс дефекта, что и отказ без строки в журнале
    (см. ``execution/pipeline.py``, отказ до ``_logger.started``).

    Формат объявлен здесь и вызывается из всех трёх выходов обработчика, чтобы
    у вызова не появилось второго формата и второго места, где его пишут.

    **Уровни: успех — DEBUG, отказ — WARNING.** Первая версия этой строки печатала
    успех на INFO, и это оказалось неверно: цикл опроса очереди вызывает
    ``claim_task`` и ``queue_stats`` каждые 10 секунд, то есть 24 строки
    ``→ ok`` в минуту и около 34 тысяч в сутки. Успешный вызов рутинной операции
    не сообщает оператору ничего, чего он не знает: платформа жива, и это уже
    видно по фактам оборота. Исчерпывающая запись каждого вызова с исходом и
    длительностью лежит в журнале (``_logger.completed``) — он и есть
    полная история, а stderr это вид. При этом отказ обязан быть виден при
    любой объявленной глубине вывода, поэтому WARNING, а не DEBUG.

    Args:
        tool_name: имя операции, как она объявлена в реестре.
        status: ``PipelineResult.status`` (``ok``/``error``/``timeout``).
        code: код отказа конвейера; пусто на успехе.
        duration_ms: длительность из результата конвейера; ``None`` — исходов,
            до которых он не дошёл (операция не найдена, сбой конвейера), и
            для них длительность **неизвестна**, а не нулевая.
    """
    detail = f"{status} {code}".strip()
    duration = f" ({duration_ms}мс)" if duration_ms is not None else ""
    line = f"вызов {tool_name} → {detail}{duration}"
    if status == "ok":
        logger.debug(line)
    else:
        logger.warning(line)


def _request_meta(server: Any) -> dict[str, Any] | None:
    """Метаданные текущего вызова из ``RequestContext`` low-level сервера.

    Метаданные приходят в запросе MCP, а не в аргументах инструмента: это
    служебный канал, недоступный модели. ``mcp`` кладёт их в
    ``request_context.meta``; собирать их из ``arguments`` было бы возвратом к
    контракту, который как раз отменяется.
    """
    request_context = getattr(server, "request_context", None)
    if request_context is None:
        return None
    meta = getattr(request_context, "meta", None)
    if isinstance(meta, dict):
        return meta
    if meta is None:
        return None
    return dict(meta)


def iter_definitions(registry: ToolRegistry) -> Iterable[ToolDefinition]:
    return iter(registry)

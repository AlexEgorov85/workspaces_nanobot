"""Опубликованная схема принимает всё, что принимает обработчик.

Требование: ``mcp-platform/libs/enterprise_common/registry.py``,
``mcp-platform/libs/enterprise_common/loader.py`` (change
``2026-10-04-journal-observability-repair``, capability ``data/operation-schema``).

Дефект, который страж держит: ``_json_type()`` при разборе объединения выбрасывал
``NoneType``, и ``priority_contents: list[str] | None = None`` публиковался как
``{"type": "array"}``. Значение ``null`` — обычный поллинг, оно законное, и
отвергалось валидатором SDK **до** конвейера: ни ``tool.started``, ни
``tool.failed``, ни одной строки в журнале. Отсутствие неотличимо от успеха.

Проверка идёт по настоящему реестру, собранному загрузчиком, и по схеме, снятой
по протоколу, а не по константе: перечень из константы обесценивает проверку —
сегодня он проходит на ``claim_task``, а завтра пропустит ту же ошибку в любой
другой из десятков операций.

Валидация не выдумывается: ``jsonschema.validate`` — ровно то, чем SDK отвергает
вызов (ветка ``input validation`` в low-level сервере MCP). Поэтому падение
инварианта даёт **достижимый класс отказа**, а не расхождение формулировок.
Внутренности SDK не трогаются: они переписываются каждым обновлением пакета, и
копия протокольной обвязки ради наблюдаемости дороже самого дефекта.
"""

from __future__ import annotations

import ast
import inspect
import types
from pathlib import Path
from typing import Any, Union, get_args, get_type_hints

import jsonschema
import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent


# -- живой реестр и живая схема ------------------------------------------------


def _registry() -> Any:
    """Реестр, собранный настоящим загрузчиком capability-файлов."""
    from servers.enterprise import server as enterprise_server

    _transport, registry, _container = enterprise_server.build()
    return registry


def _wire_schemas() -> dict[str, Any]:
    """Схемы так, как их видит клиент: по протоколу, а не из реестра."""
    import anyio

    from mcp.shared.memory import create_connected_server_and_client_session as connect
    from servers.enterprise import server as enterprise_server

    transport, _registry, _container = enterprise_server.build()

    async def _discover() -> list[Any]:
        async with connect(transport) as session:
            return (await session.list_tools()).tools

    return {tool.name: tool.inputSchema for tool in anyio.run(_discover)}


# -- проверка схемы ------------------------------------------------------------


def _field_probe(schema: dict[str, Any], name: str) -> dict[str, Any]:
    """Схема, в которой проверяется ровно одно поле.

    ``required`` убирается намеренно: иначе отказ приходит по отсутствующему
    обязательному полю, и страж сообщил бы о чужой операции. Проверяемое
    свойство — принимает ли поле значение, а не заполнен ли вызов целиком.
    """
    properties = schema.get("properties") or {}
    assert name in properties, f"параметр {name!r} не объявлен в схеме: {sorted(properties)}"
    return {"type": "object", "properties": {name: properties[name]}}


def _accepts(schema: dict[str, Any], name: str, value: Any) -> bool:
    """Прошёл бы вызов с таким значением поля валидацию SDK."""
    try:
        jsonschema.validate(instance={name: value}, schema=_field_probe(schema, name))
    except jsonschema.ValidationError:
        return False
    return True


def _accepts_null(schema: dict[str, Any], **arguments: Any) -> bool:
    """Прошёл ли вызов с такими аргументами целиком, без искусственной изоляции."""
    try:
        jsonschema.validate(instance=dict(arguments), schema=schema)
    except jsonschema.ValidationError:
        return False
    return True


# -- что обработчик принимает ---------------------------------------------------


def _is_service_param(param_name: str) -> bool:
    from libs.enterprise_common.execution.context import CONTEXT_PARAM

    return param_name in ("self", "cls", CONTEXT_PARAM)


def _nullable_params(handler: Any) -> list[str]:
    """Параметры, для которых ``None`` — законное значение.

    Два признака, и оба нужны: ``NoneType`` в аннотации (тип допускает) и дефолт
    ``None`` (вызывающая сторона так пишет). Второй важнее первого — именно им
    штатный поллинг отличается от «поля нет».
    """
    try:
        hints = get_type_hints(handler)
    except Exception:  # noqa: BLE001 - неразрешимые аннотации здесь не наш случай
        return []
    signature = inspect.signature(handler)
    found: list[str] = []
    for param_name, param in signature.parameters.items():
        if _is_service_param(param_name):
            continue
        annotation = hints.get(param_name)
        if annotation is not None:
            origin = getattr(annotation, "__origin__", None)
            if (origin is Union or origin is types.UnionType) and type(None) in get_args(
                annotation
            ):
                found.append(param_name)
                continue
        if param.default is None:
            found.append(param_name)
    return found


def _minimal_arguments(handler: Any, *, skip: str = "") -> dict[str, Any]:
    """Аргументы, которых достаточно, чтобы дойти до проверки ``null``-поля.

    Обязательные параметры заполняются заглушкой по объявленному JSON-типу:
    страж проверяет конкретный отказ на конкретном поле, и лишние отказы по
    другим полям только зашумляют сообщение.
    """
    from libs.enterprise_common.registry import _json_type

    try:
        hints = get_type_hints(handler)
    except Exception:  # noqa: BLE001
        hints = {}
    stubs = {
        "string": "проба",
        "integer": 0,
        "number": 0.0,
        "boolean": False,
        "array": [],
        "object": {},
    }
    arguments: dict[str, Any] = {}
    for param_name, param in inspect.signature(handler).parameters.items():
        if _is_service_param(param_name) or param_name == skip:
            continue
        if param.default is not inspect.Parameter.empty:
            continue
        declared = _json_type(hints.get(param_name, str))
        kind = declared[0] if isinstance(declared, list) else declared
        arguments[param_name] = stubs.get(kind, "проба")
    return arguments


def _schema_of(handler: Any) -> dict[str, Any]:
    from libs.enterprise_common.registry import build_input_schema

    return build_input_schema(handler)


# -- объявленные схемы ---------------------------------------------------------


def _source_of(definition: Any) -> Path:
    """Файл операции по модулю обработчика.

    Связь берётся из ``handler.__module__``: загрузчик импортирует capability-
    файлы под именем их пути, поэтому модуль указывает на файл однозначно, а
    перебор каталога с угадыванием имени операции давал бы ложные совпадения
    (``log_events.py`` объявляет операцию ``log_event``).
    """
    module = getattr(definition.handler, "__module__", "")
    relative = module.replace(".", "/")
    if not relative:
        return PLATFORM_ROOT / "servers/enterprise/capabilities"
    return PLATFORM_ROOT / f"{relative}.py"


def _declared_schema(path: Path) -> dict[str, Any] | None:
    """Объявленная в файле схема как есть, без исполнения файла.

    Разбор идёт деревом: исполнять файл операции ради чтения литерала означало
    бы поднять сервисы ради проверки объявления.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        if "INPUT_SCHEMA" not in names:
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            return None
        return dict(value) if isinstance(value, dict) else None
    return None


def _declared_operations() -> dict[str, tuple[dict[str, Any], Path]]:
    """Имя операции → (объявленная схема, файл). Только те, что объявили."""
    declared: dict[str, tuple[dict[str, Any], Path]] = {}
    for definition in _registry():
        path = _source_of(definition)
        if not path.is_file():
            continue
        schema = _declared_schema(path)
        if schema is not None:
            declared[definition.name] = (schema, path)
    return declared


# -- страж ---------------------------------------------------------------------


class TestPublishedSchemaMatchesHandler:
    """Одна операция, весь реестр, объявленная схема и отказ на проводе."""

    # -- инвариант: схема не уже обработчика --------------------------------

    def test_null_accepted_by_handler_is_accepted_by_schema(self) -> None:
        """Главный сценарий дефекта, названный поимённо.

        Сообщение обязано называть операцию, параметр и недопустимое значение:
        «недопустимое значение где-то» не позволяет ни починить, ни подтвердить.
        """
        schemas = _wire_schemas()
        violations: list[str] = []
        for definition in _registry():
            schema = schemas[definition.name]
            for param_name in _nullable_params(definition.handler):
                if not _accepts(schema, param_name, None):
                    violations.append(
                        f"операция {definition.name!r}, параметр {param_name!r}: "
                        f"значение null отвергнуто схемой "
                        f"{schema.get('properties', {}).get(param_name)!r}"
                    )
        assert not violations, "опубликованная схема уже обработчика:\n" + "\n".join(violations)

    def test_every_registered_operation_is_covered(self) -> None:
        """Проверяется каждая операция реестра, а не одна выборочная.

        Если бы обход молча пропускал часть файлов, страхов не было бы видно.
        Порог снизу есть: при нуле операций с nullable-параметрами обход
        перестал видеть реестр, и проверка обесценилась бы сама собой.
        """
        registry = _registry()
        schemas = _wire_schemas()
        checked = 0
        for definition in registry:
            assert definition.name in schemas, f"{definition.name} не доехал на провод"
            params = _nullable_params(definition.handler)
            if not params:
                continue
            checked += 1
            for param_name in params:
                assert _accepts(schemas[definition.name], param_name, None), (
                    f"{definition.name}.{param_name}: null отвергнут"
                )
        assert checked, (
            "ни у одной операции реестра не нашлось nullable-параметра — обход "
            "перестал видеть реестр, и проверка обесценилась"
        )

    def test_new_nullable_parameter_is_covered(self, tmp_path: Path) -> None:
        """Новая операция с nullable-параметром не приносит дефект молча.

        Операция пишется в файл и грузится настоящим ``load_definition``: путь
        «объявил автор или вывел загрузчик» — тот же, что у боевых операций.
        """
        from libs.enterprise_common.container import ToolContainer
        from libs.enterprise_common.loader import load_definition

        path = tmp_path / "probe_operation.py"
        path.write_text(
            "from libs.enterprise_common.execution.context import ToolExecutionContext\n"
            "from libs.enterprise_common.registry import ToolDefinition\n"
            "\n"
            "\n"
            "def create_tool(container):\n"
            "    def handle_probe(\n"
            "        ctx: ToolExecutionContext, mode: list[str] | None = None\n"
            "    ) -> str:\n"
            "        return 'ok'\n"
            "    return ToolDefinition(\n"
            "        name='data.probe',\n"
            "        description='Проверочная операция',\n"
            "        handler=handle_probe,\n"
            "        capability='data',\n"
            "    )\n",
            encoding="utf-8",
        )
        definition = load_definition(path, ToolContainer(services={}), tmp_path)
        assert definition.input_schema, "схема не построена — страж проверяет пустоту"
        assert _accepts(definition.input_schema, "mode", None), (
            "nullable-параметр без объявленной схемы опубликован как "
            f"{definition.input_schema.get('properties', {}).get('mode')!r}"
        )

    def test_heterogeneous_union_is_refused(self) -> None:
        """Неоднородное объединение не публикуется типом первой части.

        Раньше бралась первая часть, и модель получала заведомо неверный тип.
        Теперь загрузка падает: отказ виден сразу, а не у вызывающего на проводе.
        """
        from libs.enterprise_common.registry import ToolLoadError, build_input_schema

        def handle_probe(count: int | str) -> str:
            return "ok"

        with pytest.raises(ToolLoadError) as excinfo:
            build_input_schema(handle_probe)
        message = str(excinfo.value)
        assert "count" in message, message
        assert "handle_probe" in message, message

    def test_guard_enumerates_the_live_registry(self) -> None:
        """Перечень страж берёт у загрузчика, а не из константы.

        Сверяются три числа: операций в реестре, операций на проводе и операций,
        реально проверенных. Расхождение означало бы, что страж смотрит на
        подмножество и обесценивает сам себя.
        """
        registry = _registry()
        schemas = _wire_schemas()
        assert len(registry) == len(schemas), (
            f"на проводе {len(schemas)} операций, в реестре {len(registry)}"
        )
        assert len(registry) > 20, (
            f"в реестре всего {len(registry)} операций — перечень вырожден, "
            "проверка ничего не охраняет"
        )
        assert {definition.name for definition in registry} == set(schemas), (
            "проверены не те операции, что в реестре"
        )

    # -- объявленная схема ---------------------------------------------------

    def test_declared_schema_reaches_the_wire(self) -> None:
        """Объявленная автором схема доходит до провода поле в поле.

        Именно этого не хватало ``claim_task``: объявление было верным и
        неиспользуемым, поэтому его правка молча ничего не меняла.
        """
        schemas = _wire_schemas()
        declared = _declared_operations()
        assert declared, "ни одна операция не объявляет схему — страж проверяет пустоту"
        for name, (source, path) in declared.items():
            assert name in schemas, f"объявленная операция {name} не доехала на провод"
            assert schemas[name] == source, (
                f"{path.name}: на проводе не то, что объявлено.\n"
                f"объявлено: {source}\non wire: {schemas[name]}"
            )

    def test_absent_declaration_falls_back_to_signature(self, tmp_path: Path) -> None:
        """Без объявления схема строится из подписи, и это выражено дефолтом.

        Отдельного поля «объявлено/не объявлено» не заводится: его отсутствие —
        единственный признак, иначе у реестра появляется второе состояние, в
        котором можно забыть снять флаг.
        """
        from libs.enterprise_common.container import ToolContainer
        from libs.enterprise_common.loader import load_definition
        from libs.enterprise_common.registry import ToolDefinition

        def handle_probe(mode: str = "a") -> str:
            return "ok"

        plain = ToolDefinition(
            name="data.probe",
            description="Проверочная операция",
            handler=handle_probe,
            capability="data",
        )
        assert plain.input_schema == {}, "признак «не объявлено» изменился"
        assert not hasattr(plain, "schema_declared"), (
            "появилось отдельное поле для признака объявления — состояний стало два"
        )

        path = tmp_path / "undeclared.py"
        path.write_text(
            "from libs.enterprise_common.registry import ToolDefinition\n"
            "\n"
            "\n"
            "def create_tool(container):\n"
            "    def handle_probe(mode: str = 'a') -> str:\n"
            "        return 'ok'\n"
            "    return ToolDefinition(\n"
            "        name='data.probe',\n"
            "        description='Проверочная операция',\n"
            "        handler=handle_probe,\n"
            "        capability='data',\n"
            "    )\n",
            encoding="utf-8",
        )
        loaded = load_definition(path, ToolContainer(services={}), tmp_path)
        assert loaded.input_schema["properties"]["mode"]["type"] == "string", (
            "без объявления схема должна строиться из подписи"
        )

    def test_activated_rules_accept_real_agent_traffic(self) -> None:
        """Оживлённые правила не отвергают то, что агент шлёт на самом деле.

        ``log_events`` — операция, которой пишется весь журнал агента, и её
        объявление требует ``events[].id`` вида ``["string", "null"]``. Если
        инвариант схемы чинит отказ на проводе, а объявление при этом отвергает
        боевой трафик, то починено хуже прежнего: журнал перестаёт писаться
        вовсе.

        Форма кадра взята из ``lib/services/log_transport.py::event_to_wire``
        (либо выведена из документации операции). Импортировать агентский код
        отсюда нельзя — платформа не зависит от агента, и это страж
        ``test_architecture_boundaries.py``. Что кадр действительно имеет этот
        вид, проверяется на стороне агента:
        ``tests/test_journal_delivery_contract.py::TestJournalDeliveryContract``.
        """
        schemas = _wire_schemas()
        frame = {
            "id": "0f1d1f2c-0000-4000-8000-000000000001",
            "event_type": "agent.degraded",
            # `name` кадра — это `event_type`, а не имя операции: так его пишет
            # `lib/channels/postgres_channel.py::_journal_event` (`:524`).
            "name": "agent.degraded",
            "level": "WARN",
            "summary": "поллинг не удался",
            "payload": {"phase": "poll_inbound"},
            "metadata": {"source": "nanobot"},
            "channel": "postgres",
            "actor": "channel",
        }
        # Идентичность вызова приходит плоскими ключами аргументов, а не внутри
        # кадра: операция читает её из контекста вызова.
        assert _accepts(schemas["data.log_events"], "events", [frame]), (
            "кадр журнала агента не проходит валидацию объявленной схемой log_events"
        )
        assert _accepts_null(
            schemas["data.log_events"],
            events=[frame],
            session_id="sess-1",
            user_id="user-1",
            request_id="req-1",
        ), "вызов log_events с идентичностью отвергнут схемой"
        # Личность неидентична событию без request_id: так агент пишет события
        # вне оборота, и они не должны отвергаться по этому признаку.
        assert _accepts_null(schemas["data.log_events"], events=[{**frame, "id": None}]), (
            "log_events отвергает кадр без id — id выдаёт сервер, это законно"
        )

    def test_stale_rationale_is_removed(self) -> None:
        """Комментарий, оправдывавший затирание, переписан вместе с кодом.

        Такой комментарий завтра объяснит следующему читателю, почему правка
        «не работает», — и правка будет сделана в третий раз.
        """
        loader = (PLATFORM_ROOT / "libs/enterprise_common/loader.py").read_text(encoding="utf-8")
        assert "Схема строится здесь, а не автором файла" not in loader, (
            "loader.py до сих пор оправдывает затирание объявленной схемы"
        )
        log_events = (
            PLATFORM_ROOT / "servers/enterprise/capabilities/data/tools/log_events.py"
        ).read_text(encoding="utf-8")
        assert "схему модель не видит" in log_events, "пояснение log_events.py исчезло"
        assert "исполняется" in log_events, "log_events.py не предупреждает, что объявление живое"

    # -- отказ на проводе ----------------------------------------------------

    def test_above_pipeline_failure_has_an_owner(self) -> None:
        """Отказ выше конвейера имеет названного хозяина — агентский хук.

        Платформа отказа не видит и видеть не обязана: валидация SDK происходит
        до диспетчеризации. Хозяин называется явно, потому что «кто-то же должен
        записать» — не ответ, по которому можно проверить.
        """
        audit = (PLATFORM_ROOT.parent / "lib/hooks/tool_audit_hook.py").read_text(encoding="utf-8")
        assert "tool.failed" in audit, (
            "ToolAuditHook не пишет tool.failed — отказ выше конвейера не имеет хозяина"
        )
        assert "db_logging_service" in audit or "DbLoggingService" in audit, (
            "хук не пишет отказ в журнал — след остаётся только в консоли"
        )

    def test_sdk_internals_are_not_reached_into(self) -> None:
        """Внутренности валидатора SDK не трогаются ради наблюдаемости.

        Перехватить отказ валидации можно было бы только копией протокольной
        обвязки, которую переписывает каждое обновление SDK. Проверяются именно
        внутренности: сам пакет ``mcp`` платформе нужен — на нём держится
        единственная точка сборки сервера.
        """
        needles = ("request_handlers", "_get_cached_tool_definition", "CallToolRequest)")
        offenders: list[str] = []
        for path in sorted((PLATFORM_ROOT / "libs").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        assert not offenders, "код платформы полез во внутренности SDK:\n" + "\n".join(offenders)

    def test_invariant_violation_is_a_wire_refusal(self) -> None:
        """Падение инварианта даёт отказ на проводе, а не расхождение текстов.

        Страж должен ловить достижимый класс отказа: берём схему, в которой
        ``null`` запрещён (такой была опубликованная схема ``claim_task`` до
        правки), и убеждаемся, что валидация SDK на ней отказывает. Если
        когда-нибудь проверка перестанет воспроизводить отказ, она станет
        проверкой формы.
        """
        narrow = {
            "type": "object",
            "properties": {"priority_contents": {"type": "array", "items": {"type": "string"}}},
        }
        assert not _accepts(narrow, "priority_contents", None), (
            "историческая схема claim_task больше не отвергает null — значит, "
            "проверка инварианта измеряет не тот отказ"
        )
        assert _accepts(narrow, "priority_contents", []), "пустой список обязан проходить"
        assert _accepts(narrow, "priority_contents", ["x"]), "непустой список обязан проходить"
        assert _accepts_null(narrow), "отсутствующее поле обязано проходить"

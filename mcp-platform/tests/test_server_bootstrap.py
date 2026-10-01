"""Bootstrap сервера: fail-fast, обязательность ``sqlglot``, отсутствие
FastMCP и ручной регистрации операций.

Пункт 2.5 требует, чтобы в ``server.py`` не было ни ``@mcp.tool()``, ни
``FastMCP``: иначе добавление операции перестаёт быть добавлением файла, а на
провод уходит вторая, выведенная обёрткой схема. Пункт 2.14 — сервер не
поднимается без ``sqlglot``, потому что без AST-ветки guard пропускает
``pg_sleep``, ``information_schema`` и ``UPDATE``/``DELETE`` после ``--``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common.errors import InfrastructureError  # noqa: E402
from libs.enterprise_common.settings import Settings  # noqa: E402
from servers.enterprise import server as enterprise_server  # noqa: E402


def _settings() -> Settings:
    """Реестр поверх текущего окружения и настоящего ``platform.json``.

    Отдельная функция, чтобы проверки доезда конфигурации вызывали bootstrap
    ровно так же, как это делает сервер: реестр строит ``build()`` и
    передаёт его дальше, и проверка, строящая сервис в обход этого пути,
    не видела бы разрыва между файлом и процессом.
    """
    return Settings()


async def _discover(transport: Any) -> list[Any]:
    """Операции так, как их увидит настоящий клиент, — по протоколу."""
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async with connect(transport) as session:
        return (await session.list_tools()).tools


async def _call(transport: Any, name: str, arguments: dict[str, Any]) -> Any:
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async with connect(transport) as session:
        return await session.call_tool(name, arguments=arguments)


class TestBootstrap:
    def test_data_surface_is_stable(self) -> None:
        """Поверхность capability ``data`` фиксирована: её рост — явное решение.

        Проверяется именно ``data``, а не весь реестр. Тест «список всех
        операций сервера равен этому множеству» краснеет от каждой новой
        capability, и рано или поздно его отключат целиком — вместе с
        проверкой, которая в нём была.
        """
        _, registry, _ = enterprise_server.build()
        by_category = registry.by_category()
        assert {d.name for d in by_category["data"]} == {
            "log_event",
            "history_search",
            "schema_check",
            "claim_task",
            "update_task_status",
            # Фаза 7: контекст вопроса и очистка журнала. До этого агент писал
            # в agent_question_runs и удалял старые строки своим пулом — вторым
            # владельцем того же ресурса.
            "upsert_question_run",
            "purge_logs",
            # Фаза 7, п. 7.2: батчевый сброс буфера журнала. Без этой операции
            # агент отправлял бы по одному MCP-вызову на каждое событие
            # оборота — круговой оборот на каждый чих вместо одного на пачку.
            "log_events",
        }

    def test_every_capability_has_a_registered_service(self) -> None:
        """У каждой capability с операциями обязан быть сервис в контейнере.

        Это ловит реальный класс отказа: файл операции подложен в
        ``capabilities/<имя>/tools/``, а сервис в ``_build_container`` забыли.
        Загрузчик операцию зарегистрирует, discovery её покажет, и она будет
        падать ``InfrastructureError`` на ПЕРВОМ же вызове в проде.
        """
        _, registry, container = enterprise_server.build()
        for category in registry.by_category():
            assert container.get(category) is not None, (
                f"у capability {category!r} есть операции, но сервис не зарегистрирован"
            )

    def test_no_capability_without_operations(self) -> None:
        """Каталог capability без операций — недоделанная работа, а не заготовка.

        Пустая capability в реестре не появится, но появится её каталог, и
        следующий человек будет считать её перенесённой.
        """
        capabilities_dir = enterprise_server.CAPABILITIES_DIR
        if not capabilities_dir.is_dir():
            pytest.skip("каталог capability отсутствует")
        # ``_template`` — заготовка под новую capability, ``__pycache__`` —
        # артефакт импорта. Оба не перенос, и оставление ``_template`` в
        # этом каталоге намеренное: с него начинают новую capability.
        ignored = {"__pycache__"}
        empty = [
            entry.name
            for entry in sorted(capabilities_dir.iterdir())
            if entry.is_dir()
            and entry.name not in ignored
            and not entry.name.startswith("_")
            and not list((entry / "tools").glob("*.py"))
        ]
        assert not empty, f"capability без операций: {empty}"

    def test_runtime_only_tools_declare_permissions(self) -> None:
        """Инструмент не для модели обязан объявлять permission.

        Фильтрация model-facing инструментов живёт на стороне агента и
        работает по ``permissions``/``tags``. Операция с тегом
        ``runtime-only`` и пустым ``permissions`` не попала бы ни в один
        список и молча висела бы в реестре без владельца.
        """
        _, registry, _ = enterprise_server.build()
        for definition in registry:
            if "runtime-only" in definition.tags:
                assert definition.permissions, (
                    f"операция {definition.name!r} помечена runtime-only, "
                    "но не объявляет ни одного permission"
                )


class TestContainerWiring:
    """Конфигурация обязана доезжать до сервисов при сборке контейнера.

    Регрессия, которую этот тест закрывает: при добавлении capability ``audit``
    вызов ``_vectors_config_from_env()`` в ``_build_container`` вытеснили общей
    сборкой ``config``, и секция ``gateway.vector`` пропала. Сервер поднимался,
    операции отвечали, ``list_indexes`` отдавал **пустой** каталог — то есть
    capability работала, не объявляя ни одного индекса. Объявления доходили до
    окружения процесса, терялись в одном месте, и ни один тест этого не видел:
    все проверки строили сервис напрямую, минуя bootstrap.
    """

    def test_vector_config_reaches_the_service(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "ENTERPRISE_VECTOR_STORAGE_TABLE", "oarb.audit_vectors"
        )
        monkeypatch.setenv(
            "ENTERPRISE_VECTOR_INDEXES",
            '{"audits_index": {"table": "oarb.audits", "pk": "id"}}',
        )
        monkeypatch.setenv("ENTERPRISE_EMBED_MODEL", "mxbai-embed-large:latest")
        container = enterprise_server._build_container(_settings())
        config = container.config
        vector = ((config.get("gateway") or {}).get("vector") or {})
        index = vector.get("index") or {}
        assert index.get("storage_table") == "oarb.audit_vectors", config
        assert "audits_index" in (index.get("indexes") or {}), config
        assert (vector.get("embedding") or {}).get("model") == "mxbai-embed-large:latest"

    def test_audit_config_reaches_the_service(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "ENTERPRISE_SCRIPTS_REGISTRY_TABLE", "public.agent_predefined_scripts"
        )
        monkeypatch.setenv(
            "ENTERPRISE_AUDIT_TABLES", "oarb.audits, oarb.violations\noarb.audit_reports"
        )
        monkeypatch.setenv("ENTERPRISE_AUDIT_ROW_CEILING", "500")
        container = enterprise_server._build_container(_settings())
        config = container.config
        assert config["scripts_registry"]["table"] == "public.agent_predefined_scripts"
        # Разделители: запятая с пробелом и перевод строки. Список пишут руками.
        assert config["audit"]["tables"] == [
            "oarb.audits",
            "oarb.violations",
            "oarb.audit_reports",
        ]
        # Число, а не строка: потолок строк — счётчик, и раньше он доезжал
        # строкой, которую сервис аудита приводил сам. Приводит теперь реестр,
        # и потолок приходит числом туда же, где проверяется на тип.
        assert config["audit"]["row_ceiling"] == 500

    def test_both_sections_coexist(self, monkeypatch) -> None:
        """Секции не должны затирать друг друга.

        Отдельный тест, а не часть предыдущих: слияние словарей на месте
        затирания — ровно тот способ, которым одна пропавшая секция убила
        другую.
        """
        monkeypatch.setenv("ENTERPRISE_VECTOR_STORAGE_TABLE", "oarb.audit_vectors")
        monkeypatch.setenv("ENTERPRISE_SCRIPTS_REGISTRY_TABLE", "public.agent_predefined_scripts")
        config = enterprise_server._build_container(_settings()).config
        assert config.get("gateway"), "секция gateway потеряна"
        assert config.get("scripts_registry"), "секция scripts_registry потеряна"
        assert config.get("audit"), "секция audit потеряна"
        assert config.get("statement_timeout_ms") is not None

    def test_no_sql_surface_on_operations(self) -> None:
        """Произвольного SQL на поверхности агента не существует."""
        _, registry, _ = enterprise_server.build()
        for definition in registry:
            for param in definition.input_schema["properties"]:
                assert param not in {"sql", "query_sql", "statement", "raw_sql", "sql_text", "ddl"}

    def test_container_exposes_data_service(self) -> None:
        _, _, container = enterprise_server.build()
        assert container.get("data") is not None

    def test_transport_is_lowlevel_server(self) -> None:
        from mcp.server.lowlevel import Server

        transport, _, _ = enterprise_server.build()
        assert isinstance(transport, Server)
        assert callable(transport.run)


class TestWireContract:
    """Контракт проверяется по протоколу, а не по внутренним структурам."""

    def test_operations_are_discoverable(self) -> None:
        transport, registry, _ = enterprise_server.build()
        tools = pytest.importorskip("anyio").run(_discover, transport)
        assert {t.name for t in tools} == set(registry.names())

    def test_every_operation_is_documented(self) -> None:
        transport, _, _ = enterprise_server.build()
        for tool in pytest.importorskip("anyio").run(_discover, transport):
            assert tool.description, f"у операции {tool.name} нет описания"

    def test_wire_schema_is_exactly_the_registry_schema(self) -> None:
        """На проводе ровно одна схема — та, что провалидирована при загрузке.

        С ``FastMCP`` их было две: реестровая и выведенная обёрткой. Расхождение
        проявлялось как ``kwargs`` в схеме и падение валидации на нормальном
        вызове.
        """
        import anyio

        transport, registry, _ = enterprise_server.build()
        tools = {t.name: t for t in anyio.run(_discover, transport)}
        for definition in registry:
            wire = tools[definition.name].inputSchema
            assert wire.get("properties") == dict(definition.input_schema["properties"])
            assert sorted(wire.get("required", [])) == sorted(definition.input_schema["required"])
            assert "kwargs" not in wire.get("properties", {})
            assert "args" not in wire.get("properties", {})

    def test_history_search_exposes_full_filter_set(self) -> None:
        """Фильтры поиска видны на проводе, а не только в реестре.

        Агентский адаптер ``history_search`` сохраняет модельную
        поверхность, поэтому ``tool_name`` и ``until`` обязаны быть в
        схеме операции: иначе вызов отвергнут валидацией до входа в
        обработчик, и фильтр молча перестанет работать.
        """
        import anyio

        transport, _, _ = enterprise_server.build()
        tools = {t.name: t for t in anyio.run(_discover, transport)}
        props = tools["history_search"].inputSchema["properties"]
        assert {"tool_name", "until", "event_type", "level", "since", "query"} <= set(props)

    def test_call_returns_text(self) -> None:
        import anyio

        transport, _, _ = enterprise_server.build()
        result = anyio.run(
            _call,
            transport,
            "log_event",
            {"event_type": "smoke.contract", "summary": "проверка"},
        )
        assert not result.isError
        assert "accepted" in result.content[0].text

    def test_domain_error_carries_code_and_no_traceback(self) -> None:
        import anyio

        transport, _, _ = enterprise_server.build()
        result = anyio.run(_call, transport, "log_event", {"event_type": "   "})
        text = result.content[0].text
        assert result.isError is True
        assert "invalid_request" in text
        assert "Traceback" not in text

    def test_unknown_operation_is_reported_as_error(self) -> None:
        import anyio

        transport, _, _ = enterprise_server.build()
        result = anyio.run(_call, transport, "no_such_operation", {})
        assert result.isError is True
        assert "Traceback" not in result.content[0].text

    def test_argument_validation_uses_wire_schema(self) -> None:
        """Отсутствующий обязательный параметр отсекается протоколом."""
        import anyio

        transport, _, _ = enterprise_server.build()
        result = anyio.run(_call, transport, "log_event", {})
        assert result.isError is True


class TestSqlglotIsMandatory:
    def test_missing_sqlglot_stops_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Без AST-ветки guard проверяет только первый оператор.

        Подниматься «на всякий случай» нельзя: сервер, работающий с ослабленной
        защитой, опаснее сервера, который не поднялся.
        """
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "sqlglot":
                raise ImportError("sqlglot отсутствует")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(InfrastructureError, match="sqlglot"):
            enterprise_server._check_dependencies()

    def test_dependency_list_names_sqlglot(self) -> None:
        assert "sqlglot" in enterprise_server.REQUIRED_PACKAGES

    def test_missing_dsn_stops_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Сервер без DSN выглядит рабочим, пока журнал пуст.

        Буфер при этом честно теряет каждое событие, но агент об этом не узнаёт.
        Молчаливая потеря журнала хуже отказа на старте.
        """
        from libs.enterprise_data import db as data_db

        monkeypatch.setattr(data_db, "_dsn", "")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("PG_DSN", raising=False)
        with pytest.raises(InfrastructureError, match="не задан DSN"):
            enterprise_server._check_dependencies()


class TestNoManualToolRegistration:
    def test_server_has_no_tool_decorators(self) -> None:
        """Операции приходят файлами; декоратор означал бы правку server.py."""
        source = (PLATFORM_ROOT / "servers" / "enterprise" / "server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"tool", "add_tool"}:
                    pytest.fail("в server.py не должно быть ручной регистрации операций")

    def test_adding_tool_file_requires_no_server_change(self, tmp_path: Path) -> None:
        """Новая операция — это новый файл, а не правка bootstrap'а."""
        from libs.enterprise_common.container import ToolContainer
        from libs.enterprise_common.loader import discover_tool_files, load_registry

        tools_dir = tmp_path / "data" / "tools"
        tools_dir.mkdir(parents=True)
        (tools_dir / "brand_new.py").write_text(
            "from libs.enterprise_common.registry import ToolDefinition\n"
            "\n"
            "def handle(value: str) -> str:\n"
            "    return value\n"
            "\n"
            "def create_tool(container):\n"
            "    return ToolDefinition(name='brand_new', description='Новая.', handler=handle, category='data')\n",
            encoding="utf-8",
        )
        assert [p.name for p in discover_tool_files(tmp_path)] == ["brand_new.py"]
        registry = load_registry(tmp_path, ToolContainer(), root=tmp_path)
        assert "brand_new" in registry

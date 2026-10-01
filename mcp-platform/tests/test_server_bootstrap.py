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
import logging
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


def _main_statements() -> list[str]:
    """Тела верхнего уровня ``main`` как список распарсенных выражений.

    AST, а не поиск подстроки: определение функции содержит её же имя, и
    страж на ``source.index(...)`` принимал удаление вызова за «вызов на
    месте». Здесь утверждение есть только если вызов действительно есть.
    """
    tree = ast.parse(Path(enterprise_server.__file__).read_text(encoding="utf-8"))
    main = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main"
    )
    return [ast.unparse(statement) for statement in main.body]


def _loop_statement_index() -> int:
    """Позиция оператора верхнего уровня ``main``, где запускается loop.

    Внутри него может лежать вложенный ``_serve`` (он объявлен в ``try``),
    поэтому ищем по развёрнутому тексту оператора, а не по узлу функции.
    """
    statements = _main_statements()
    for index, statement in enumerate(statements):
        if "anyio.run(_serve)" in statement:
            return index
    raise AssertionError("в main нет оператора с anyio.run(_serve)")


class TestHeavyImportWarmup:
    """Прогрев numpy/pandas до старта loop.

    Дефект, который он закрывает: DuckDB тянет эти пакеты лениво на первом
    же ``execute`` с параметрами, а обработчики операций идут в воркерах
    AnyIO — импорт оттуда зависает намертво (на живом процессе больше
    180 с вместо 0,2 с).
    """

    def test_warmup_imports_packages(self) -> None:
        for name in ("numpy", "pandas"):
            sys.modules.pop(name, None)

        enterprise_server._warm_heavy_imports()

        for name in ("numpy", "pandas"):
            assert name in sys.modules, f"{name} не прогрет"

    def test_warmup_survives_missing_package(self, monkeypatch) -> None:
        """Отсутствие пакета не должно ронять старт: DuckDB работает и без
        pandas, и прогрев — оптимизация, а не условие подъёма."""
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "pandas":
                raise ImportError("нет pandas")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)

        enterprise_server._warm_heavy_imports()

    def test_warmup_runs_before_loop_starts(self) -> None:
        statements = _main_statements()
        loop = _loop_statement_index()

        assert "_warm_heavy_imports()" in statements, (
            "прогрев тяжёлых импортов не вызывается в main"
        )
        assert statements.index("_warm_heavy_imports()") < loop, (
            "прогрев после старта loop: первый запрос снова платит за импорт из воркера"
        )


class TestStartupPreparation:
    """Подготовка capability до старта loop: ленивых загрузок быть не должно.

    Инвариант владельца: на старте сервера все операции доступны быстро.
    Без этого шага первая же операция платит за реестр и за сборку FAISS, и
    это выглядит как «индексы не ищутся» — дефект там, где его нет.
    """

    class _Audit:
        def __init__(self, result: Any = None, error: Exception | None = None):
            self.calls = 0
            self._result = result if result is not None else {"count": 6}
            self._error = error

        def list_scripts(self) -> Any:
            self.calls += 1
            if self._error is not None:
                raise self._error
            return self._result

    class _Owner:
        def __init__(self, error: Exception | None = None):
            self.built: list[str] = []
            self._error = error

        def ensure_index(self, name: str) -> None:
            self.built.append(name)
            if self._error is not None:
                raise self._error

    class _Vectors:
        def __init__(self, declared: list[str], owner: Any):
            self.owner = owner
            self._declared = declared

        def list_indexes(self) -> list[dict[str, Any]]:
            return [{"index_name": name, "declared": True} for name in self._declared]

        def state(self, name: str) -> str:
            return "ready"

    class _Container:
        def __init__(self, **services: Any):
            self.services = services

    def test_registry_is_read_at_startup(self) -> None:
        audit = self._Audit()
        enterprise_server._prepare_capabilities(self._Container(audit=audit))

        assert audit.calls == 1, "реестр скриптов обязан читаться на старте"

    def test_declared_indexes_are_built_at_startup(self) -> None:
        vectors = self._Vectors(["audits_index", "violations_index"], self._Owner())

        enterprise_server._prepare_capabilities(self._Container(vectors=vectors))

        assert vectors.owner.built == ["audits_index", "violations_index"]

    def test_broken_registry_does_not_stop_startup(self) -> None:
        """Снимок по контракту необязателен: подняться без него — законное
        состояние. Но отказ обязан быть назван, а не проглочен."""
        audit = self._Audit(error=RuntimeError("снимок недоступен"))

        enterprise_server._prepare_capabilities(self._Container(audit=audit))

        assert audit.calls == 1

    def test_failed_index_build_does_not_stop_startup(self) -> None:
        owner = self._Owner(error=RuntimeError("нет эмбеддера"))
        vectors = self._Vectors(["audits_index"], owner=owner)

        enterprise_server._prepare_capabilities(self._Container(vectors=vectors))

        assert owner.built == ["audits_index"]

    def test_missing_services_are_not_an_error(self) -> None:
        """Процесс ``--capabilities llm`` поднимает один сервис."""
        enterprise_server._prepare_capabilities(self._Container())

    def test_startup_log_reports_real_index_state(self, caplog) -> None:
        """В баннер идёт настоящее состояние индекса.

        Зашитое ``ready`` выглядело бы как «всё поднялось» там, где индекс
        собрать не удалось, — то есть молчаливое враньё вместо отказа.
        """

        class _Broken(self._Vectors):
            def state(self, name: str) -> str:
                return "error"

        vectors = _Broken(["audits_index"], self._Owner())

        with caplog.at_level(logging.INFO):
            enterprise_server._prepare_capabilities(self._Container(vectors=vectors))

        assert "error" in caplog.text, "состояние индекса в логе не отражено"

    def test_preparation_runs_before_loop_starts(self) -> None:
        statements = _main_statements()
        loop = _loop_statement_index()

        assert "_prepare_capabilities(container)" in statements, (
            "подготовка capability не вызывается в main"
        )
        assert statements.index("_prepare_capabilities(container)") < loop, (
            "подготовка capability после старта loop: ленивая загрузка вернулась на горячий путь"
        )


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

    def test_audit_config_reaches_the_service(self) -> None:
        """Объявление доезжает до capability из файла, а не из окружения.

        Раньше проверка ставила переменные окружения и ждала их в контейнере.
        Теперь источник — ``platform.json``: переменная окружения тут не
        может нести метку, а значит не может сказать «это реестр».
        """
        container = enterprise_server._build_container(_settings())
        config = container.config
        registry = config["scripts_registry"]["table"]
        assert registry == "public.agent_predefined_scripts", config
        # Реестр помечен и в доменные таблицы не попадает.
        assert registry not in config["audit"]["tables"], config
        assert config["audit"]["tables"] == [
            "oarb.audits",
            "oarb.violations",
            "oarb.audit_reports",
            "oarb.report_items",
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
            enterprise_server._check_dependencies(Settings())

    def test_dependency_list_names_sqlglot(self) -> None:
        assert "sqlglot" in enterprise_server.REQUIRED_PACKAGES

    def test_missing_dsn_stops_server(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Сервер без DSN выглядит рабочим, пока журнал пуст.

        Буфер при этом честно теряет каждое событие, но агент об этом не узнаёт.
        Молчаливая потеря журнала хуже отказа на старте.

        DSN приходит из ``platform.json`` через подстановки, поэтому «нет DSN»
        — это пустой ключ в файле плюс пустое окружение: локальный
        ``.secrets.env`` подставил бы значение и проверка стала бы зелёной
        вхолостую.
        """
        import json

        from libs.enterprise_data import db as data_db

        monkeypatch.setattr(data_db, "_dsn", "")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("PG_DSN", raising=False)

        raw = json.loads(
            (PLATFORM_ROOT / "platform.json").read_text(encoding="utf-8")
        )
        raw["db"]["dsn"] = ""
        no_dsn = tmp_path / "platform.json"
        no_dsn.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        with pytest.raises(InfrastructureError, match="не задан DSN"):
            enterprise_server._check_dependencies(
                # Подстановка ключа провайдера разворачивается из окружения:
                # без неё разбор файла остановился бы на ней, и проверка DSN
                # не была бы проверена вовсе. На путь DSN это не влияет —
                # ``db.dsn`` в файле пуст, а ``DATABASE_URL`` в окружении нет.
                Settings(env={"EMBED_TOKEN": "test",
        "LLM_API_KEY": "test"}, secrets={}, file_path=no_dsn)
            )


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

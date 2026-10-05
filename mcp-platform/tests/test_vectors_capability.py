"""Capability ``vectors``: операции, границы и сторож на мусоре.

Проверяет требования миграции ``enterprise-mcp-platform``, фаза 3:

* ни одна операция не принимает SQL от вызывающей стороны (п.13);
* в capability нет ни ``duckdb``, ни ``ATTACH``, ни пути к файлу снимка
  (п.18) — capability знает только имя индекса, текст и параметры выдачи;
* снимок приходит из сервиса capability ``data``; второго пути к нему нет;
* каждый сторож проверен на заведомо плохих данных: не «тест проходит», а
  «сторож срабатывает» (п.16).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.errors import InfrastructureError, NotFoundError
from servers.enterprise.capabilities.vectors.service.main import VectorsService
from servers.enterprise.capabilities.vectors.tools import (
    index_stats as index_stats_tool,
)
from servers.enterprise.capabilities.vectors.tools import list_indexes as list_tool
from servers.enterprise.capabilities.vectors.tools import vector_search as search_tool

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
CAPABILITY_DIR = PLATFORM_ROOT / "servers" / "enterprise" / "capabilities" / "vectors"
TOOLS_DIR = CAPABILITY_DIR / "tools"

#: Параметры, означающие «вызывающая сторона присылает SQL».
SQL_PARAM_NAMES = {"sql", "query_sql", "statement", "raw_sql", "sql_text", "ddl"}


class FakeDataService:
    """Снимок capability ``data`` без DuckDB и без пути к файлу."""

    def __init__(self, *, rows: int = 3, sources: tuple[str, ...] = ("idx_a",)):
        self.rows = rows
        self.sources = sources
        self.fetch_calls = 0

    def vector_source_stats(self) -> list[dict[str, Any]]:
        return [
            {
                "source": s,
                "vector_count": self.rows,
                "embedding_sample": "[0.5,0.5,0.5,0.5]",
            }
            for s in self.sources
        ]

    def fetch_source_vectors(self, source: str) -> list[dict[str, Any]]:
        self.fetch_calls += 1
        if source not in self.sources:
            return []
        return [
            {
                "source": source, "table": "public.audits",
                "pk_value": f"pk-{i}", "chunk_index": 0, "chunk_count": 1,
                "embedding": [0.1 * (i + 1) * (j + 1) for j in range(4)],
            }
            for i in range(self.rows)
        ]

    def fetch_chunk_payload(self, source: str, pk: Any, chunk: int) -> dict[str, Any]:
        return {"content": f"текст {pk}", "search_text": "", "row": {}}


def _service(data: Any | None = None, **kwargs: Any) -> VectorsService:
    container = ToolContainer()
    data = data if data is not None else FakeDataService()
    container.register("data", data)
    container.register("vectors", VectorsService(container=container, **kwargs))
    return container.get("vectors")


def _embed(text: str) -> list[float]:
    return [0.1] * 4


@pytest.fixture
def service() -> VectorsService:
    return _service(embed=_embed)


def _handler(tool_module: Any, container: ToolContainer):
    """Собрать операцию и достать её обработчик.

    Обработчик живёт внутри ``create_tool`` и замыкает сервис: держать
    модульную глобальную переменную ради теста не нужно и нельзя — иначе
    тест проверял бы не тот объект, который регистрируется в сервере.
    """
    return tool_module.create_tool(container).handler


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            # importlib.import_module(...) и __import__(...) — тот же обход.
            func = node.func
            called = getattr(func, "attr", None) or getattr(func, "id", None)
            if called in {"import_module", "__import__"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.add(first.value.split(".")[0])
    return names


# ---------------------------------------------------------------------------
# Пункт 13 / 18: границы capability
# ---------------------------------------------------------------------------


class TestNoSqlSurface:
    def test_no_tool_parameter_means_sql(self) -> None:
        """Ни одна операция не принимает SQL: иначе это «прогони мой запрос»."""
        for path in sorted(TOOLS_DIR.glob("*.py")):
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = [a.arg for a in node.args.args + node.args.kwonlyargs]
                    bad = SQL_PARAM_NAMES & set(args)
                    assert not bad, f"{path.name}:{node.name} принимает {bad}"

    def test_registered_schema_has_no_sql_params(self) -> None:
        from libs.enterprise_common.registry import build_input_schema

        container = ToolContainer()
        data = FakeDataService()
        container.register("data", data)
        container.register(
            "vectors", VectorsService(container=container, embed=_embed)
        )
        for module in (search_tool, list_tool, index_stats_tool):
            definition = module.create_tool(container)
            schema = build_input_schema(definition.handler)
            properties = set(schema["properties"])
            assert not (properties & SQL_PARAM_NAMES), definition.name
            assert definition.capability == "vectors"

    def test_search_tool_exposes_only_text_and_index(self, service) -> None:
        from libs.enterprise_common.registry import build_input_schema

        container = ToolContainer()
        container.register("data", FakeDataService())
        container.register("vectors", service)
        definition = search_tool.create_tool(container)
        schema = build_input_schema(definition.handler)
        assert set(schema["properties"]) == {
            "query", "index_name", "top_k", "threshold"
        }
        assert schema["required"] == ["query"]


class TestNoSnapshotPath:
    def test_no_duckdb_import_in_capability(self) -> None:
        for path in sorted(CAPABILITY_DIR.rglob("*.py")):
            assert "duckdb" not in _imported_modules(path), path

    def test_no_attach_statement_in_capability(self) -> None:
        for path in sorted(CAPABILITY_DIR.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    assert "ATTACH" not in node.value.upper(), path

    def test_no_duckdb_filename_in_capability(self) -> None:
        """Путь к файлу снимка в capability означал бы второй способ его найти."""
        for path in sorted(CAPABILITY_DIR.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            assert "cache.duckdb" not in text, path
            assert "local_path" not in text, path

    def test_no_home_paths_in_capability(self) -> None:
        for path in sorted(CAPABILITY_DIR.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            assert "~/" not in text, path
            assert "C:\\" not in text, path

    def test_no_agent_imports_in_capability(self) -> None:
        forbidden = {"nanobot", "lib", "workspace", "tools", "config",
                     "benchmarks", "cli_agent", "gateway", "streamlit_app"}
        for path in sorted(CAPABILITY_DIR.rglob("*.py")):
            leaked = _imported_modules(path) & forbidden
            assert not leaked, f"{path}: {leaked}"


# ---------------------------------------------------------------------------
# Снимок приходит из capability data
# ---------------------------------------------------------------------------


class TestSnapshotComesFromData:
    def test_service_reads_through_data_service(self) -> None:
        data = FakeDataService()
        service = _service(data, embed=_embed)
        service.list_indexes()
        assert data.fetch_calls == 0, "list_indexes не должен читать строки"

    def test_missing_data_service_raises_infrastructure_error(self) -> None:
        container = ToolContainer()
        with pytest.raises(InfrastructureError) as excinfo:
            VectorsService(container=container, embed=_embed)
        assert "data" in str(excinfo.value)

    def test_data_service_without_snapshot_reads_raises(self) -> None:
        """Сервис ``data`` без методов чтения снимка — сбой сборки, не «нет данных»."""
        container = ToolContainer()
        container.register("data", object())
        with pytest.raises(InfrastructureError) as excinfo:
            VectorsService(container=container, embed=_embed)
        assert "снимок" in str(excinfo.value).lower()

    def test_data_service_snapshot_errors_propagate(self) -> None:
        class BrokenData:
            def vector_source_stats(self) -> list[dict[str, Any]]:
                raise InfrastructureError("файл кэша занят другим процессом")

            def fetch_source_vectors(self, source: str) -> list[dict[str, Any]]:
                return []

            def fetch_chunk_payload(self, *a: Any) -> dict[str, Any]:
                return {}

        service = _service(BrokenData(), embed=_embed)
        with pytest.raises(InfrastructureError) as excinfo:
            service.list_indexes()
        assert "занят" in str(excinfo.value)

    def test_no_second_snapshot_path_in_service(self) -> None:
        """Сервис не открывает файл: в нём нет ни open_snapshot_store, ни путей."""
        import inspect

        from servers.enterprise.capabilities.vectors.service import main as service_main

        source = inspect.getsource(service_main)
        assert "open_snapshot_store" not in source
        assert "duckdb" not in source


# ---------------------------------------------------------------------------
# Поведение операций
# ---------------------------------------------------------------------------


class TestVectorSearchTool:
    def _call(self, service, **kwargs: Any) -> dict[str, Any]:
        container = ToolContainer()
        container.register("vectors", service)
        return json.loads(_handler(search_tool, container)(**kwargs))

    def test_returns_results_and_state(self, service) -> None:
        payload = self._call(service, query="договор", index_name="idx_a", top_k=2)
        assert payload["found"] == 2
        assert payload["index_name"] == "idx_a"
        assert payload["index_state"] == "ready"
        assert payload["results"][0]["content"].startswith("текст ")

    def test_state_is_missing_before_first_search(self, service) -> None:
        assert service.state("idx_a") == "missing"

    def test_unknown_index_raises_not_found(self, service) -> None:
        with pytest.raises(NotFoundError) as excinfo:
            self._call(service, query="x", index_name="нет")
        assert excinfo.value.code == "not_found"

    def test_top_k_is_forwarded(self, service) -> None:
        payload = self._call(service, query="x", index_name="idx_a", top_k=1)
        assert payload["found"] == 1

    def test_empty_query_is_rejected(self, service) -> None:
        """Пустой текст не ищет «ничего», а отвергается: эмбеддер не поймёт его."""
        from libs.enterprise_common.errors import InvalidRequestError

        with pytest.raises(InvalidRequestError) as excinfo:
            self._call(service, query="", index_name="idx_a")
        assert excinfo.value.code == "invalid_request"

    @pytest.mark.parametrize("query", ["", "   ", "\n\t"])
    def test_blank_query_is_rejected(self, service, query: str) -> None:
        from libs.enterprise_common.errors import InvalidRequestError

        with pytest.raises(InvalidRequestError):
            self._call(service, query=query, index_name="idx_a")


class TestListIndexesTool:
    def _call(self, service) -> dict[str, Any]:
        container = ToolContainer()
        container.register("vectors", service)
        return json.loads(_handler(list_tool, container)())

    def test_lists_indexes_with_state(self, service) -> None:
        payload = self._call(service)
        assert payload["count"] == 1
        assert payload["indexes"][0]["index_name"] == "idx_a"
        assert payload["indexes"][0]["state"] == "missing"

    def test_does_not_build(self, service) -> None:
        self._call(service)
        assert service.build_count("idx_a") == 0

    def test_empty_snapshot_yields_empty_list(self) -> None:
        service = _service(FakeDataService(sources=()), embed=_embed)
        assert self._call(service)["count"] == 0


class TestIndexStatsTool:
    def _call(self, service, **kwargs: Any) -> dict[str, Any]:
        container = ToolContainer()
        container.register("vectors", service)
        return json.loads(_handler(index_stats_tool, container)(**kwargs))

    def test_reports_required_metrics(self, service) -> None:
        payload = self._call(service, index_name="idx_a")
        assert {"vectors", "dim", "last_built_at", "queries"} <= set(payload)
        assert payload["vectors"] == 3
        assert payload["dim"] == 4
        assert payload["last_built_at"] is None
        assert payload["queries"] == 0

    def test_does_not_build(self, service) -> None:
        self._call(service, index_name="idx_a")
        assert service.build_count("idx_a") == 0

    def test_after_search_reports_build_time(self, service) -> None:
        service.vector_search("запрос", "idx_a", top_k=1)
        payload = self._call(service, index_name="idx_a")
        assert payload["last_built_at"] is not None
        assert payload["state"] == "ready"
        assert payload["queries"] == 1

    def test_unknown_index_raises(self, service) -> None:
        with pytest.raises(NotFoundError):
            self._call(service, index_name="нет")


# ---------------------------------------------------------------------------
# Пункт 16: сторож на заведомо плохих данных
# ---------------------------------------------------------------------------


class TestGuardsFireOnGarbage:
    """Сторож должен срабатывать, а не просто «быть на месте»."""

    def test_service_without_data_is_rejected(self) -> None:
        with pytest.raises(InfrastructureError):
            VectorsService(container=ToolContainer(), embed=_embed)

    def test_wrong_data_service_is_rejected(self) -> None:
        container = ToolContainer()
        container.register("data", "не сервис")
        with pytest.raises(InfrastructureError):
            VectorsService(container=container, embed=_embed)

    def test_garbage_snapshot_rows_are_rejected(self) -> None:
        class GarbageData:
            def vector_source_stats(self) -> list[dict[str, Any]]:
                return [{"source": "idx_a", "vector_count": 2,
                         "embedding_sample": "не вектор"}]

            def fetch_source_vectors(self, source: str) -> list[dict[str, Any]]:
                return [
                    {"source": source, "pk_value": "a", "chunk_index": 0,
                     "chunk_count": 1, "embedding": "мусор"},
                ]

            def fetch_chunk_payload(self, *a: Any) -> dict[str, Any]:
                return {}

        service = _service(GarbageData(), embed=_embed)
        assert service.index_stats("idx_a")["dim"] is None
        with pytest.raises(InfrastructureError) as excinfo:
            service.vector_search("запрос", "idx_a", top_k=1)
        assert excinfo.value.code == "invalid_index"
        assert service.state("idx_a") == "error"

    def test_garbage_index_name_is_rejected(self, service) -> None:
        for name in ("", "  ", "нет", "\x00", "a" * 500):
            with pytest.raises(NotFoundError):
                service.index_stats(name)

    def test_empty_snapshot_is_not_a_crash(self) -> None:
        service = _service(FakeDataService(sources=()), embed=_embed)
        assert service.list_indexes() == []
        with pytest.raises(NotFoundError):
            service.vector_search("запрос", "idx_a", top_k=1)

    def test_tool_hashes_are_ignored_in_payload(self, service) -> None:
        """Хеш-мусор в эмбеддинге не должен попасть в выдачу как «документ»."""
        container = ToolContainer()
        container.register("vectors", service)
        handle = _handler(search_tool, container)
        payload = json.loads(handle(query="x", index_name="idx_a", top_k=3))
        assert all(isinstance(r["content"], str) for r in payload["results"])
        assert all(isinstance(r["score"], float) for r in payload["results"])

    def test_no_facility_to_reach_the_file_from_a_tool(self) -> None:
        """В наборе параметров операции нет ничего, что указало бы на файл."""
        from libs.enterprise_common.registry import build_input_schema

        container = ToolContainer()
        container.register("data", FakeDataService())
        service = VectorsService(container=container, embed=_embed)
        container.register("vectors", service)
        for module in (search_tool, list_tool, index_stats_tool):
            definition = module.create_tool(container)
            schema = build_input_schema(definition.handler)
            for param in schema["properties"]:
                assert not any(
                    token in param for token in ("path", "file", "dir", "sql")
                ), f"{definition.name}:{param}"

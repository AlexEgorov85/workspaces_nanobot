"""Bootstrap сервера: fail-fast, обязательность ``sqlglot``, отсутствие
ручного ``@mcp.tool()``.

Пункт 2.5 требует, чтобы в ``server.py`` не было ни одного ``@mcp.tool()``:
иначе добавление операции перестаёт быть добавлением файла. Пункт 2.14 —
сервер не поднимается без ``sqlglot``, потому что без AST-ветки guard
пропускает ``pg_sleep``, ``information_schema`` и ``UPDATE``/``DELETE`` после
``--``-комментария.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common.errors import InfrastructureError  # noqa: E402
from servers.enterprise import server as enterprise_server  # noqa: E402


class TestBootstrap:
    def test_build_registers_expected_operations(self) -> None:
        _, registry, _ = enterprise_server.build()
        assert set(registry.names()) == {
            "log_event",
            "history_search",
            "schema_check",
            "claim_task",
            "update_task_status",
        }

    def test_all_operations_belong_to_data(self) -> None:
        _, registry, _ = enterprise_server.build()
        assert set(registry.by_category()) == {"data"}

    def test_no_sql_surface_on_operations(self) -> None:
        """Произвольного SQL на поверхности агента не существует."""
        _, registry, _ = enterprise_server.build()
        for definition in registry:
            for param in definition.input_schema["properties"]:
                assert param not in {"sql", "query_sql", "statement", "raw_sql", "sql_text", "ddl"}

    def test_container_exposes_data_service(self) -> None:
        _, _, container = enterprise_server.build()
        assert container.get("data") is not None

    def test_module_exposes_mcp_attribute(self) -> None:
        """Контракт для архитектурного теста: сервер поднимается под именем ``mcp``."""
        assert hasattr(enterprise_server, "mcp")


class TestSqlglotIsMandatory:
    def test_missing_sqlglot_stops_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Без AST-ветки guard проверяет только первый оператор.

        Подниматься «на всякий случай» нельзя: сервер, работающий с ослабленной
        защитой, опаснее сервера, который не поднялся.
        """
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "sqlglot":
                raise ImportError("sqlglot отсутствует")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("builtins.__import__", fake_import)
        with pytest.raises(InfrastructureError, match="sqlglot"):
            enterprise_server._check_dependencies()

    def test_dependency_list_names_sqlglot(self) -> None:
        assert "sqlglot" in enterprise_server.REQUIRED_PACKAGES


class TestNoManualToolDecorators:
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

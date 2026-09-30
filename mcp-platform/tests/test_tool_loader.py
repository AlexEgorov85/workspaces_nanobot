"""Загрузчик операций: discovery и fail-fast валидация.

Пункт 2.4 требует тест на **каждый** пункт валидации. Ниже они перечислены
явными именами, чтобы пропуск был виден в diff'е, а не спрятан в общем тесте.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from libs.enterprise_common.container import ToolContainer  # noqa: E402
from libs.enterprise_common.loader import (  # noqa: E402
    discover_tool_files,
    load_definition,
    load_registry,
)
from libs.enterprise_common.registry import ToolLoadError  # noqa: E402

GOOD_TOOL = '''
from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition


def handle(text: str) -> str:
    """Обработчик."""
    return text


def create_tool(container: ToolContainer) -> ToolDefinition:
    return ToolDefinition(
        name="{name}",
        description="Операция {name}.",
        handler=handle,
        category="{category}",
    )
'''


def _write(root: Path, capability: str, name: str, body: str) -> Path:
    tools_dir = root / capability / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    path = tools_dir / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "servers" / "enterprise").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def container() -> ToolContainer:
    return ToolContainer(services={})


class TestDiscovery:
    def test_finds_tools_files(self, root: Path) -> None:
        _write(root, "data", "op_a", GOOD_TOOL.format(name="op_a", category="data"))
        _write(root, "audit", "op_b", GOOD_TOOL.format(name="op_b", category="audit"))
        found = discover_tool_files(root)
        # Порядок — по полному пути, то есть по имени capability: ``audit``
        # раньше ``data``. Фиксируем именно это, чтобы смена алфавита была
        # видна как изменение теста, а не как «вдруг переставилось».
        assert [p.name for p in found] == ["op_b.py", "op_a.py"]

    def test_skips_dunder_and_private(self, root: Path) -> None:
        _write(root, "data", "op", GOOD_TOOL.format(name="op", category="data"))
        _write(root, "data", "__init__", "")
        _write(root, "data", "_private", "")
        found = discover_tool_files(root)
        assert [p.name for p in found] == ["op.py"]

    def test_order_is_stable(self, root: Path) -> None:
        for name in ("zeta", "alpha", "mid"):
            _write(root, "data", name, GOOD_TOOL.format(name=name, category="data"))
        assert [p.name for p in discover_tool_files(root)] == [
            "alpha.py",
            "mid.py",
            "zeta.py",
        ]

    def test_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert discover_tool_files(tmp_path / "absent") == []


class TestLoadDefinitionHappyPath:
    def test_loads_and_builds_schema(self, root: Path, container: ToolContainer) -> None:
        path = _write(root, "data", "op", GOOD_TOOL.format(name="op", category="data"))
        definition = load_definition(path, container, root)
        assert definition.name == "op"
        assert definition.category == "data"
        assert definition.input_schema["required"] == ["text"]
        assert definition.input_schema["properties"]["text"]["type"] == "string"


class TestValidationPoints:
    """По одному тесту на каждый пункт валидации из 2.3."""

    def test_import_error_fails(self, root: Path, container: ToolContainer) -> None:
        path = _write(root, "data", "broken", "import definitely_not_a_module_xyz\n")
        with pytest.raises(ToolLoadError, match="не импортируется"):
            load_definition(path, container, root)

    def test_missing_entry_point_fails(self, root: Path, container: ToolContainer) -> None:
        path = _write(root, "data", "no_entry", "value = 1\n")
        with pytest.raises(ToolLoadError, match="нет точки входа create_tool"):
            load_definition(path, container, root)

    def test_entry_point_not_callable_fails(self, root: Path, container: ToolContainer) -> None:
        path = _write(root, "data", "bad_entry", "create_tool = 42\n")
        with pytest.raises(ToolLoadError, match="обязан быть вызываемым"):
            load_definition(path, container, root)

    def test_entry_point_raising_fails(self, root: Path, container: ToolContainer) -> None:
        body = (
            "def create_tool(container):\n"
            "    raise RuntimeError('нечем собрать сервис')\n"
        )
        path = _write(root, "data", "raises", body)
        with pytest.raises(ToolLoadError, match="create_tool упал"):
            load_definition(path, container, root)

    def test_wrong_return_type_fails(self, root: Path, container: ToolContainer) -> None:
        body = "def create_tool(container):\n    return {'name': 'x'}\n"
        path = _write(root, "data", "dict_return", body)
        with pytest.raises(ToolLoadError, match="обязан вернуть ToolDefinition"):
            load_definition(path, container, root)

    def test_empty_name_fails(self, root: Path, container: ToolContainer) -> None:
        path = _write(root, "data", "empty", GOOD_TOOL.format(name="x", category="data").replace('name="x"', 'name="  "'))
        with pytest.raises(ToolLoadError, match="имя операции не должно быть пустым"):
            load_definition(path, container, root)

    def test_empty_description_fails(self, root: Path, container: ToolContainer) -> None:
        path = _write(
            root, "data", "nodesc", GOOD_TOOL.format(name="x", category="data").replace('description="Операция x."', 'description=""')
        )
        with pytest.raises(ToolLoadError, match="описание операции не должно быть пустым"):
            load_definition(path, container, root)

    def test_non_callable_handler_fails(self, root: Path, container: ToolContainer) -> None:
        body = (
            "from libs.enterprise_common.registry import ToolDefinition\n"
            "\n"
            "def create_tool(container):\n"
            "    return ToolDefinition(name='x', description='d', handler=42, category='data')\n"
        )
        path = _write(root, "data", "bad_handler", body)
        with pytest.raises(ToolLoadError, match="handler обязан быть вызываемым"):
            load_definition(path, container, root)

    def test_invalid_argument_schema_fails(self, root: Path, container: ToolContainer) -> None:
        body = (
            "from libs.enterprise_common.registry import ToolDefinition\n"
            "\n"
            "def handle(text):\n"
            "    return text\n"
            "\n"
            "def create_tool(container):\n"
            "    return ToolDefinition(name='x', description='d', handler=handle, category='data')\n"
        )
        path = _write(root, "data", "no_annotations", body)
        with pytest.raises(ToolLoadError, match="без аннотации типа"):
            load_definition(path, container, root)

    def test_duplicate_name_fails_with_path(self, root: Path, container: ToolContainer) -> None:
        """Ошибка обязана называть того, кто занял чужое имя, а не того, кто первым.

        Уже зарегистрированная операция законна; виноват новый файл, и
        именно его путь должен попасть в сообщение.
        """
        _write(root, "data", "first", GOOD_TOOL.format(name="same", category="data"))
        _write(root, "audit", "second", GOOD_TOOL.format(name="same", category="audit"))
        order = discover_tool_files(root)
        assert [p.name for p in order] == ["second.py", "first.py"]
        with pytest.raises(ToolLoadError) as excinfo:
            load_registry(root, container, root=root)
        assert order[-1].name in str(excinfo.value)
        assert "same" in str(excinfo.value)
        assert "уже зарегистрировано" in str(excinfo.value)


class TestFailFast:
    def test_one_bad_file_fails_whole_registry(self, root: Path, container: ToolContainer) -> None:
        """Частично загруженный сервер хуже незагруженного.

        Он принимает соединение, а половина операций отсутствует, и это
        обнаруживается в проде на конкретном вызове.
        """
        _write(root, "data", "good", GOOD_TOOL.format(name="good", category="data"))
        _write(root, "data", "zz_bad", "import definitely_not_a_module_xyz\n")
        with pytest.raises(ToolLoadError):
            load_registry(root, container, root=root)

    def test_error_names_the_failing_file(self, root: Path, container: ToolContainer) -> None:
        _write(root, "data", "good", GOOD_TOOL.format(name="good", category="data"))
        _write(root, "audit", "bad", "value = 1\n")
        with pytest.raises(ToolLoadError) as excinfo:
            load_registry(root, container, root=root)
        assert "bad.py" in str(excinfo.value)


class TestRegistryAssembly:
    def test_all_valid_tools_registered(self, root: Path, container: ToolContainer) -> None:
        _write(root, "data", "op_a", GOOD_TOOL.format(name="op_a", category="data"))
        _write(root, "audit", "op_b", GOOD_TOOL.format(name="op_b", category="audit"))
        registry = load_registry(root, container, root=root)
        assert registry.names() == ("op_a", "op_b")
        assert set(registry.by_category()) == {"audit", "data"}

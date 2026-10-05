"""Загрузчик операций: discovery и fail-fast валидация.

Пункт 2.4 требует тест на **каждый** пункт валидации. Ниже они перечислены
явными именами, чтобы пропуск был виден в diff'е, а не спрятан в общем тесте.

Фикстуры пишут файлы под ``capabilities/<capability>/tools/`` — с корнем
``capabilities``. Это не оформление: имя capability сверяется с каталогом
**от этого корня**, и файл, положенный в ``<capability>/tools/`` без него, дал
бы пустое имя capability, сверка молча не сработала бы, и весь класс про
расхождение с каталогом остался бы зелёным, ничего не проверяя.
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

#: Корень каталогов capability в тестовом дереве — то же имя, что и в
#: ``loader.CAPABILITIES_DIRNAME``, и по той же причине объявлено здесь, а не
#: выведено из соглашения.
CAPABILITIES_DIRNAME = "capabilities"

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
        capability="{capability}",
    )
'''


def _write(root: Path, capability: str, name: str, body: str) -> Path:
    """Положить файл операции в ``capabilities/<capability>/tools/``.

    Имя файла и объявленное имя — разные вещи, и ``name`` здесь именно имя
    файла: иначе объявление с чужим capability невозможно положить рядом со
    своим файлом, а проверка расхождения стала бы недостижимой.
    """
    tools_dir = root / CAPABILITIES_DIRNAME / capability / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    path = tools_dir / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return path


def _good(root: Path, capability: str, filename: str, operation: str | None = None) -> Path:
    """Файл с годным объявлением: имя в форме, поле совпадает с каталогом."""
    return _write(
        root,
        capability,
        filename,
        GOOD_TOOL.format(name=operation or f"{capability}.{filename}", capability=capability),
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "servers" / "enterprise").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def container() -> ToolContainer:
    return ToolContainer(services={})


class TestDiscovery:
    def test_finds_tools_files(self, root: Path) -> None:
        _good(root, "data", "op_a")
        _good(root, "audit", "op_b")
        found = discover_tool_files(root / CAPABILITIES_DIRNAME)
        # Порядок — по полному пути, то есть по имени capability: ``audit``
        # раньше ``data``. Фиксируем именно это, чтобы смена алфавита была
        # видна как изменение теста, а не как «вдруг переставилось».
        assert [p.name for p in found] == ["op_b.py", "op_a.py"]

    def test_skips_dunder_and_private(self, root: Path) -> None:
        _good(root, "data", "op")
        _write(root, "data", "__init__", "")
        _write(root, "data", "_private", "")
        found = discover_tool_files(root / CAPABILITIES_DIRNAME)
        assert [p.name for p in found] == ["op.py"]

    def test_order_is_stable(self, root: Path) -> None:
        for name in ("zeta", "alpha", "mid"):
            _good(root, "data", name)
        assert [p.name for p in discover_tool_files(root / CAPABILITIES_DIRNAME)] == [
            "alpha.py",
            "mid.py",
            "zeta.py",
        ]

    def test_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert discover_tool_files(tmp_path / "absent") == []


class TestLoadDefinitionHappyPath:
    def test_loads_and_builds_schema(self, root: Path, container: ToolContainer) -> None:
        path = _good(root, "data", "op")
        definition = load_definition(path, container, root)
        assert definition.name == "data.op"
        assert definition.capability == "data"
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
        path = _write(
            root, "data", "empty", GOOD_TOOL.format(name="data.x", capability="data").replace('name="data.x"', 'name="  "')
        )
        with pytest.raises(ToolLoadError, match="имя операции не должно быть пустым"):
            load_definition(path, container, root)

    def test_empty_description_fails(self, root: Path, container: ToolContainer) -> None:
        path = _write(
            root,
            "data",
            "nodesc",
            GOOD_TOOL.format(name="data.x", capability="data").replace(
                'description="Операция data.x."', 'description=""'
            ),
        )
        with pytest.raises(ToolLoadError, match="описание операции не должно быть пустым"):
            load_definition(path, container, root)

    def test_non_callable_handler_fails(self, root: Path, container: ToolContainer) -> None:
        body = (
            "from libs.enterprise_common.registry import ToolDefinition\n"
            "\n"
            "def create_tool(container):\n"
            "    return ToolDefinition(name='data.x', description='d', handler=42, capability='data')\n"
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
            "    return ToolDefinition(name='data.x', description='d', handler=handle, capability='data')\n"
        )
        path = _write(root, "data", "no_annotations", body)
        with pytest.raises(ToolLoadError, match="без аннотации типа"):
            load_definition(path, container, root)

    def test_duplicate_name_fails_with_path(self, root: Path, container: ToolContainer) -> None:
        """Ошибка обязана называть того, кто занял чужое имя, а не того, кто первым.

        Уже зарегистрированная операция законна; виноват новый файл, и
        именно его путь должен попасть в сообщение.

        Оба файла лежат в каталоге одной capability и объявляют одно имя:
        после переезда разные capability дали бы разные именя по построению
        (префикс имени и есть capability), и проверка уникальности на
        пересечении capability стала бы недостижимой.
        """
        _write(root, "data", "first", GOOD_TOOL.format(name="data.same", capability="data"))
        _write(root, "data", "second", GOOD_TOOL.format(name="data.same", capability="data"))
        order = discover_tool_files(root / CAPABILITIES_DIRNAME)
        assert [p.name for p in order] == ["first.py", "second.py"]
        with pytest.raises(ToolLoadError) as excinfo:
            load_registry(root / CAPABILITIES_DIRNAME, container, root=root)
        assert order[-1].name in str(excinfo.value)
        assert "data.same" in str(excinfo.value)
        assert "уже зарегистрировано" in str(excinfo.value)


class TestCapabilityMatchesCapabilityDirectory:
    """``capability`` — имя capability, и оно сверяется с каталогом.

    Сторон у расхождения две: имя capability из пути и объявленное поле. Обе
    обязаны быть в сообщении — иначе непонятно, что чинить: каталог или
    объявление. Проверяются оба исхода, а не только успешный: страж, который
    ни разу не срабатывал, неотличим от стража, который ничего не проверяет.

    В каждом отказе имя и поле **согласованы между собой** (префикс ``audit`` и
    поле ``audit``) и расходятся только с каталогом ``data``. Иначе отказ
    пришёл бы раньше, от сверки префикса имени с полем, и класс доказывал бы
    не то правило.
    """

    def test_matching_capability_loads(self, root: Path, container: ToolContainer) -> None:
        path = _good(root, "audit", "op")
        assert load_definition(path, container, root).capability == "audit"

    def test_foreign_capability_rejected(self, root: Path, container: ToolContainer) -> None:
        path = _write(
            root, "data", "op", GOOD_TOOL.format(name="audit.op", capability="audit")
        )
        with pytest.raises(ToolLoadError) as excinfo:
            load_definition(path, container, root)
        message = str(excinfo.value)
        assert "не совпадает с capability" in message
        assert "capability 'audit'" in message, message
        assert "capability 'data'" in message, message
        assert path.name in message

    def test_foreign_capability_stops_the_registry(
        self, root: Path, container: ToolContainer
    ) -> None:
        """В реестр операция не попадает: загрузка прерывается целиком.

        Проверяется на целом реестре, а не на одной операции, потому что
        «не зарегистрировалась» и «зарегистрировалась под чужой capability» —
        разные исходы, и второй без сверки выглядел бы как успех.
        """
        _good(root, "data", "good")
        _write(
            root,
            "data",
            "zz_foreign",
            GOOD_TOOL.format(name="audit.zz_foreign", capability="audit"),
        )
        with pytest.raises(ToolLoadError, match="не совпадает с capability"):
            load_registry(root / CAPABILITIES_DIRNAME, container, root=root)

    def test_foreign_capability_rejected_under_capability_filter(
        self, root: Path, container: ToolContainer
    ) -> None:
        """Фильтр ``--capabilities`` не обходит сверку.

        Имя для сравнения берётся из пути, поэтому файл из ``data/tools`` с
        ``capability="audit"`` отвергается и при ``capabilities=["data"]``: если
        бы сверка смотрела на объявление, файл прошёл бы как «свой» и расхождение
        уехало бы в реестр под чужой capability.
        """
        _write(root, "data", "op", GOOD_TOOL.format(name="audit.op", capability="audit"))
        _good(root, "audit", "other")
        with pytest.raises(ToolLoadError, match="не совпадает с capability"):
            load_registry(
                root / CAPABILITIES_DIRNAME, container, root=root, capabilities=["data"]
            )

    def test_empty_capability_is_not_a_mismatch(
        self, root: Path, container: ToolContainer
    ) -> None:
        """Пустая capability и чужая — разные отказы.

        Сверка с каталогом не подменяет проверку непустоты: «не сказано» и
        «сказано не то» чинятся разными правками, и второй отказ не должен
        приходить на место первого. Имя здесь годное — ``data.op`` — иначе
        отказ пришёл бы от проверки формы, а не от непустоты.
        """
        _write(root, "data", "op", GOOD_TOOL.format(name="data.op", capability=""))
        with pytest.raises(ToolLoadError, match=r"capability не должна быть пустой"):
            load_registry(root / CAPABILITIES_DIRNAME, container, root=root)


class TestFailFast:
    def test_one_bad_file_fails_whole_registry(self, root: Path, container: ToolContainer) -> None:
        """Частично загруженный сервер хуже незагруженного.

        Он принимает соединение, а половина операций отсутствует, и это
        обнаруживается в проде на конкретном вызове.
        """
        _good(root, "data", "good")
        _write(root, "data", "zz_bad", "import definitely_not_a_module_xyz\n")
        with pytest.raises(ToolLoadError):
            load_registry(root / CAPABILITIES_DIRNAME, container, root=root)

    def test_error_names_the_failing_file(self, root: Path, container: ToolContainer) -> None:
        _good(root, "data", "good")
        _write(root, "audit", "bad", "value = 1\n")
        with pytest.raises(ToolLoadError) as excinfo:
            load_registry(root / CAPABILITIES_DIRNAME, container, root=root)
        assert "bad.py" in str(excinfo.value)


class TestRegistryAssembly:
    def test_all_valid_tools_registered(self, root: Path, container: ToolContainer) -> None:
        _good(root, "data", "op_a")
        _good(root, "audit", "op_b")
        registry = load_registry(root / CAPABILITIES_DIRNAME, container, root=root)
        assert registry.names() == ("audit.op_b", "data.op_a")
        assert set(registry.by_capability()) == {"audit", "data"}

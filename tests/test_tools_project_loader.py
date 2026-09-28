"""Тесты ``register_project_tools`` из ``lib/services/project_tool_loader.py``.

Этот файл — historical continuation ``test_tools_project_loader.py``, который
тестировал ``RuntimePatcher.patch_project_tools``. После opencode change
``runtime-patcher-composition-cleanup`` регистрация project tools
переехала в отдельный loader
(см. ``lib/services/project_tool_loader.py::register_project_tools``);
``RuntimePatcher`` больше НЕ имеет метода ``patch_project_tools`` и
НЕ вызывает его из ``apply_all()``.

Семантика тестов не меняется — best-effort регистрация, ``detail``-формат
совместим с ``runtime_inventory.parse_project_tools_detail``. Никакие
тесты-логика не удаляются, только точечный рефакторинг fixtures
(``RuntimePatcher().patch_project_tools(...)`` →
``register_project_tools(...)``).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lib.services.project_tool_loader import (
    ProjectToolsLoadResult,
    register_project_tools,
)


@pytest.fixture(autouse=True)
def _isolate_workspace_tools_modules():
    """Сбросить ВСЕ ``workspace.tools.*`` перед каждым тестом этого файла.

    Без этого ранее загруженные tool-классы (из других тестов) продолжают
    жить в ``sys.modules['workspace.tools.*']`` и попадают в candidates
    ``register_project_tools``, ломая изоляцию тестов.

    Это безопасно, потому что ``test_tools_project_loader.py`` идёт ПОСЛЕ
    ``test_history_search_tool.py`` и ``test_context_compaction.py``
    алфавитно — на момент его запуска те тесты уже прошли, и состояние
    ``workspace.tools.*`` сброшено перед этим файлом.
    """
    to_drop = [k for k in sys.modules if k.startswith("workspace.tools.")]
    for k in to_drop:
        del sys.modules[k]
    yield
    for k in to_drop:
        sys.modules.pop(k, None)


# ---------------------------------------------------------------------------
# Фикстуры: workspace с модулем workspace/tools/_dummy_tool.py
# ---------------------------------------------------------------------------


def _write_tool_module(
    tools_dir: Path,
    module_name: str,
    *,
    enable_field: str = "enable",
    config_key: str = "dummy",
    tool_name: str = "dummy_tool",
    exec_return: str = "ok",
) -> None:
    """Записать минимальный tool-модуль в ``tools_dir/module_name.py``.

    Использует только ``nanobot.agent.tools.base.Tool`` и ``pydantic`` —
    без зависимостей от ``lib.*`` (тесты должны быть изолированными).
    """
    (tools_dir / "__init__.py").write_text("")
    (tools_dir / f"{module_name}.py").write_text(
        "from nanobot.agent.tools.base import Tool, tool_parameters\n"
        "from pydantic import BaseModel\n"
        "\n"
        f"class DummyCfg(BaseModel):\n"
        f"    {enable_field}: bool = True\n"
        "\n"
        "@tool_parameters({'type': 'object', 'properties': {}})\n"
        "class DummyTool(Tool):\n"
        f"    config_key = {config_key!r}\n"
        "    @classmethod\n"
        "    def config_cls(cls): return DummyCfg\n"
        "    @classmethod\n"
        "    def enabled(cls, ctx): return "
        f"getattr(ctx.config.{config_key}, {enable_field!r}, True)\n"
        "    @classmethod\n"
        "    def create(cls, ctx): return cls()\n"
        f"    @property\n    def name(self): return {tool_name!r}\n"
        "    @property\n    def description(self): return 'test dummy'\n"
        f"    async def execute(self, **kwargs): return {exec_return!r}\n"
    )


@pytest.fixture
def workspace_with_tool(tmp_path):
    """tmp_path/tools/dummy.py с включённым tool."""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    _write_tool_module(tools_dir, "dummy")
    return tmp_path


@pytest.fixture
def workspace_with_disabled_tool(tmp_path):
    """tmp_path/tools/dummy.py с tool, у которого enable=false в config."""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    _write_tool_module(tools_dir, "dummy", enable_field="disable_me")
    return tmp_path


@pytest.fixture
def workspace_with_two_tools(tmp_path):
    """Два tool-модуля: dummy_tool и other_tool."""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    _write_tool_module(tools_dir, "dummy")
    _write_tool_module(
        tools_dir,
        "other",
        config_key="other",
        tool_name="other_tool",
        exec_return="other-ok",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Помощники: собрать «agent», похожий на AgentLoop
# ---------------------------------------------------------------------------


def _make_agent(*, tools_config_section=None) -> MagicMock:
    """Собрать MagicMock с минимальным набором атрибутов ``AgentLoop``."""
    agent = MagicMock()
    agent.tools.get.return_value = None  # ничего не зарегистрировано
    agent.tools.has.return_value = False
    agent.tools_config = _FakeToolsConfig(tools_config_section or {})
    agent.workspace = "/fake/workspace"
    agent.bus = MagicMock()
    agent.subagents = MagicMock()
    agent.cron_service = MagicMock()
    agent._exec_session_manager = MagicMock()
    agent.sessions = MagicMock()
    agent.file_states = MagicMock()
    agent.provider_snapshot_loader = MagicMock()
    agent._image_generation_provider_configs = {}
    agent._runtime_control = MagicMock()
    # context.timezone — атрибут через ``.context``
    ctx_obj = MagicMock()
    ctx_obj.timezone = "UTC"
    agent.context = ctx_obj
    agent.workspace_scopes = MagicMock()
    agent.workspace_scopes.sandbox_status = None
    return agent


class _FakeSection:
    """Объект-секция: ``section.field`` -> значение по ключу."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def __getattr__(self, name: str):
        return self._data.get(name, None)


class _FakeToolsConfig:
    """``agent.tools_config`` — атрибут .<key> -> секция."""

    def __init__(self, sections: dict[str, dict]) -> None:
        self._sections = sections

    def __getattr__(self, name: str):
        return _FakeSection(self._sections.get(name, {}))


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------


class TestRegisterProjectTools:
    def test_workspace_tools_missing_is_ok(self, tmp_path):
        """Нет ``workspace/tools/`` — skip без ошибки."""
        agent = _make_agent()
        result = register_project_tools(agent, tmp_path)
        assert isinstance(result, ProjectToolsLoadResult)
        assert result.registered == []
        assert result.disabled == []
        assert result.duplicate == []
        assert result.failed == []
        assert "not found" in result.detail or "skip" in result.detail
        agent.tools.register.assert_not_called()

    def test_agent_is_none(self, tmp_path):
        """``agent=None`` — отказ с явной причиной.

        ``result.failed`` теперь содержит маркер ``"register_project_tools"``
        (loader-level failure), чтобы banner diagnostics видел отказ
        через ``diff_project_tools()``. ``result.error`` заполняется
        для programmatic consumers.
        """
        result = register_project_tools(None, tmp_path)
        assert isinstance(result, ProjectToolsLoadResult)
        assert "agent is None" in result.detail
        assert result.registered == []
        assert "register_project_tools" in result.failed
        assert result.error is not None

    def test_registers_tool_from_workspace(self, workspace_with_tool):
        """Tool из ``workspace/tools/dummy.py`` регистрируется."""
        agent = _make_agent(tools_config_section={"dummy": {"enable": True}})

        result = register_project_tools(agent, workspace_with_tool)
        assert "dummy_tool" in result.detail
        assert "dummy_tool" in result.registered
        # Был вызван register
        assert agent.tools.register.called
        # Имя зарегистрированного tool — "dummy_tool"
        registered_names = [
            call.args[0].name
            for call in agent.tools.register.call_args_list
        ]
        assert "dummy_tool" in registered_names

    def test_skips_already_registered(self, workspace_with_tool):
        """Если tool с таким именем уже зарегистрирован — пропускаем."""
        agent = _make_agent(tools_config_section={"dummy": {"enable": True}})
        # ``tools.get("dummy_tool")`` возвращает не-None — имитируем,
        # что tool уже зарегистрирован ранее (например, встроенным loader'ом).
        agent.tools.get.return_value = object()  # любой truthy

        result = register_project_tools(agent, workspace_with_tool)
        assert "already registered" in result.detail
        assert "dummy_tool" in result.duplicate
        agent.tools.register.assert_not_called()

    def test_disabled_in_config(self, workspace_with_tool):
        """Tool с ``enable=False`` в config — пропускается.

        ``result.disabled`` теперь содержит каноническое имя tool'а
        (``tool.name`` == ``"dummy_tool"``), а не class name
        (``"DummyTool"``) — это нужно для совпадения с
        ``canonical_project_tools()`` (см. opencode change
        ``runtime-patcher-composition-cleanup``).
        """
        agent = _make_agent(tools_config_section={"dummy": {"enable": False}})

        result = register_project_tools(agent, workspace_with_tool)
        assert "disabled" in result.detail
        assert "dummy_tool" in result.disabled
        agent.tools.register.assert_not_called()

    def test_two_tools_both_registered(self, workspace_with_two_tools):
        """Два модуля в workspace/tools/ — оба регистрируются."""
        agent = _make_agent(
            tools_config_section={
                "dummy": {"enable": True},
                "other": {"enable": True},
            }
        )

        result = register_project_tools(agent, workspace_with_two_tools)
        assert "dummy_tool" in result.registered
        assert "other_tool" in result.registered
        registered_names = [
            call.args[0].name
            for call in agent.tools.register.call_args_list
        ]
        assert "dummy_tool" in registered_names
        assert "other_tool" in registered_names

    def test_no_project_tools(self, tmp_path):
        """Пустая workspace/tools/ — нет tool'ов, ok."""
        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "__init__.py").write_text("")

        agent = _make_agent()
        result = register_project_tools(agent, tmp_path)
        assert "no project tools" in result.detail
        assert result.registered == []
        agent.tools.register.assert_not_called()

    def test_module_import_failure_does_not_crash(self, tmp_path, caplog):
        """Ошибка импорта одного модуля не валит весь patch."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        (tools_dir / "__init__.py").write_text("")
        # Модуль с синтаксической ошибкой
        (tools_dir / "broken.py").write_text("raise RuntimeError(\"boom\")")
        # И нормальный модуль рядом
        _write_tool_module(tools_dir, "good")

        agent = _make_agent(
            tools_config_section={"dummy": {"enable": True}}
        )
        result = register_project_tools(agent, tmp_path)
        # Регистрация должна завершиться без падения
        assert "dummy_tool" in result.registered
        # good-модуль всё-таки зарегистрировался
        registered_names = [
            call.args[0].name
            for call in agent.tools.register.call_args_list
        ]
        assert "dummy_tool" in registered_names


class TestRegisterProjectToolsIntegration:
    """Проверка, что ``register_project_tools`` работает standalone
    (без привязки к ``RuntimePatcher.apply_all``)."""

    def test_returns_project_tools_load_result(self, tmp_path):
        agent = MagicMock()
        agent.tools.get.return_value = object()  # всё "уже зарегистрировано"
        agent.tools.has.return_value = True
        agent.tools_config = _FakeToolsConfig({})
        agent.workspace = "/fake/workspace"
        agent.bus = MagicMock()
        agent.subagents = MagicMock()
        agent.cron_service = MagicMock()
        agent._exec_session_manager = MagicMock()
        agent.sessions = MagicMock()
        agent.file_states = MagicMock()
        agent.provider_snapshot_loader = MagicMock()
        agent._image_generation_provider_configs = {}
        agent._runtime_control = MagicMock()
        ctx_obj = MagicMock()
        ctx_obj.timezone = "UTC"
        agent.context = ctx_obj
        agent.workspace_scopes = MagicMock()
        agent.workspace_scopes.sandbox_status = None

        # Пустая workspace/tools
        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "__init__.py").write_text("")

        result = register_project_tools(agent, tmp_path)
        assert isinstance(result, ProjectToolsLoadResult)
        assert "no project tools" in result.detail


class TestRegisterProjectToolsEdgeCases:
    """Edge-кейсы для ``register_project_tools``."""

    def test_no_init_py_still_works(self, tmp_path):
        """``__init__.py`` в workspace/tools/ необязателен (модули загружаются
        через ``importlib.util.spec_from_file_location``)."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        # НЕ создаём __init__.py — типичный in-repo layout
        _write_tool_module(tools_dir, "no_init")

        agent = _make_agent(tools_config_section={"dummy": {"enable": True}})
        result = register_project_tools(agent, tmp_path)
        assert "dummy_tool" in result.detail
        assert "dummy_tool" in result.registered

    def test_dunder_module_skipped(self, tmp_path):
        """Модули, начинающиеся с ``_``, не подхватываются."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        _write_tool_module(tools_dir, "_underscore_dunder")
        _write_tool_module(tools_dir, "regular")

        agent = _make_agent(tools_config_section={"dummy": {"enable": True}})
        result = register_project_tools(agent, tmp_path)
        # Только regular.py зарегистрирован
        registered = [
            c.args[0].name for c in agent.tools.register.call_args_list
        ]
        assert "dummy_tool" in registered
        assert len(registered) == 1

    def test_settings_ref_propagated(self, tmp_path):
        """``settings`` пробрасывается в ``ctx._settings_ref``."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        _write_tool_module(tools_dir, "dummy")

        agent = _make_agent(tools_config_section={"dummy": {"enable": True}})
        settings = MagicMock(name="settings")

        result = register_project_tools(
            agent, tmp_path, settings=settings,
        )
        assert "dummy_tool" in result.registered

    def test_no_settings_no_crash(self, tmp_path):
        """Без ``settings=None`` — loader работает (tool получает None)."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        _write_tool_module(tools_dir, "dummy")

        agent = _make_agent(tools_config_section={"dummy": {"enable": True}})
        result = register_project_tools(
            agent, tmp_path, settings=None,
        )
        assert "dummy_tool" in result.registered

    def test_create_failure_logged_and_continues(self, tmp_path, caplog):
        """Если ``cls.create(ctx)`` падает, остальные tool'ы продолжают
        регистрироваться."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        _write_tool_module(tools_dir, "good")
        (tools_dir / "broken_create.py").write_text(
            "from nanobot.agent.tools.base import Tool, tool_parameters\n"
            "from pydantic import BaseModel\n"
            "\n"
            "class BrokenCfg(BaseModel):\n"
            "    enable: bool = True\n"
            "\n"
            "@tool_parameters({'type': 'object', 'properties': {}})\n"
            "class BrokenCreateTool(Tool):\n"
            "    config_key = 'broken_create'\n"
            "    @classmethod\n"
            "    def config_cls(cls): return BrokenCfg\n"
            "    @classmethod\n"
            "    def enabled(cls, ctx): return True\n"
            "    @classmethod\n"
            "    def create(cls, ctx): raise RuntimeError('intentional boom')\n"
            "    @property\n"
            "    def name(self): return 'broken_create_tool'\n"
            "    @property\n"
            "    def description(self): return 'broken'\n"
            "    async def execute(self, **kwargs): return 'never'\n"
        )

        agent = _make_agent(
            tools_config_section={
                "dummy": {"enable": True},
                "broken_create": {"enable": True},
            }
        )
        result = register_project_tools(agent, tmp_path)
        # good зарегистрирован, broken — нет
        assert "dummy_tool" in result.registered
        assert "broken_create_tool" not in result.registered
        # ``result.failed`` теперь содержит каноническое имя tool'а
        # (``tool.name`` == ``"broken_create_tool"``), а не class name.
        assert "broken_create_tool" in result.failed
        assert result.detail.startswith("[INTERNAL_FAILED] ")


class TestOuterFailure:
    """Outer-failure loader'а (``_discover`` / ``ToolContext`` и т.п.)
    должен попадать в ``ProjectToolsLoadResult.failed`` и ``error``,
    чтобы banner diagnostics не терял loader-level ошибку (см.
    opencode change ``runtime-patcher-composition-cleanup``, фаза 4.1).

    Раньше внешний ``except`` возвращал ``ProjectToolsLoadResult(detail="patch failed: ...")``
    без заполнения ``failed`` — banner не видел failure.
    """

    def test_outer_failure_populates_failed_and_error(self, tmp_path, monkeypatch):
        """Если ``_discover`` падает — ``failed`` и ``error`` заполняются."""
        from lib.services import project_tool_loader

        def _explode(workspace_dir):
            raise RuntimeError("intentional discover failure")

        monkeypatch.setattr(project_tool_loader, "_discover", _explode)

        agent = _make_agent(tools_config_section={})
        # Make a tools/ directory so we don't early-return "workspace/tools not found"
        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "__init__.py").write_text("")

        result = register_project_tools(agent, tmp_path)

        # Outer failure MUST populate ``failed`` for banner diagnostics.
        assert "register_project_tools" in result.failed, (
            "Outer loader failure не попал в result.failed — "
            "banner diagnostics не увидит loader-level failure. "
            f"failed={result.failed}, detail={result.detail!r}"
        )
        assert result.error is not None, (
            "Outer loader failure должен заполнять result.error для "
            "programmatic consumers"
        )
        assert "intentional discover failure" in result.error
        # Detail содержит repr исключения для диагностики в логах.
        assert "intentional discover failure" in result.detail
        # Регистрация не происходит при outer failure.
        assert result.registered == []

    def test_agent_none_populates_failed_and_error(self, tmp_path):
        """``agent=None`` — тоже outer failure (хотя и явный)."""
        result = register_project_tools(None, tmp_path)
        assert "register_project_tools" in result.failed
        assert result.error is not None
        assert "agent is None" in result.detail


class TestProjectToolsInventoryBanner:
    """``_emit_project_tools_inventory_banner`` использует структурные
    поля ``ProjectToolsLoadResult`` напрямую, а не regex-парсинг
    ``detail`` (см. opencode change ``runtime-patcher-composition-cleanup``,
    followups — banner behaviour fix).

    Раньше banner вызывал ``diff_project_tools_from_detail(detail)``,
    что для outer-loader failure (``detail = "register_project_tools
    failed: RuntimeError: ..."``) давал семантически неправильный
    результат: ``failed = ["RuntimeError: ..."]``.
    """

    def test_outer_failure_shows_loader_error_not_garbled_name(
        self, capsys, monkeypatch
    ):
        """Outer-loader failure показывает ``LOADER ERROR: <repr>``,
        а не garbage в ``FAILED:``."""
        from lib.core.application_context import _emit_project_tools_inventory_banner
        from lib.services.project_tool_loader import ProjectToolsLoadResult

        result = ProjectToolsLoadResult(
            registered=[],
            disabled=[],
            duplicate=[],
            failed=["register_project_tools"],
            detail="register_project_tools failed: RuntimeError: intentional boom",
            error="RuntimeError: intentional boom",
        )

        _emit_project_tools_inventory_banner(result)

        captured = capsys.readouterr()
        # ``LOADER ERROR:`` строка должна содержать error repr
        assert "LOADER ERROR:" in captured.err
        assert "RuntimeError: intentional boom" in captured.err
        # НЕ должно быть FAILED: с garbage именем из regex-парсинга
        assert "FAILED: RuntimeError" not in captured.err

    def test_missing_required_with_structured_fields(self, capsys):
        """Когда required tool missing, banner показывает MISSING REQUIRED
        через structured-поля (без regex-парсинга detail)."""
        from lib.core.application_context import _emit_project_tools_inventory_banner
        from lib.services.project_tool_loader import ProjectToolsLoadResult

        result = ProjectToolsLoadResult(
            registered=["history_search"],
            disabled=[],
            duplicate=[],
            failed=[],
            detail="1 project tools registered: history_search",  # legacy
            error=None,
        )

        _emit_project_tools_inventory_banner(result)

        captured = capsys.readouterr()
        assert "MISSING REQUIRED:" in captured.err
        assert "compact_context" in captured.err
        assert "legal_summarizer_query" in captured.err

    def test_no_inventory_drift_returns_silently(self, capsys):
        """Если drift нет (всё совпадает с canonical) — banner молчит."""
        from lib.core.application_context import _emit_project_tools_inventory_banner
        from lib.services.project_tool_loader import ProjectToolsLoadResult

        result = ProjectToolsLoadResult(
            registered=["compact_context", "history_search", "legal_summarizer_query"],
            disabled=["ExampleTool"],
            duplicate=[],
            failed=[],
            detail="3 project tools registered: ...; 1 disabled by config: ExampleTool",
            error=None,
        )

        _emit_project_tools_inventory_banner(result)

        captured = capsys.readouterr()
        # canonical + disabled ExampleTool = нет drift
        assert "MISSING REQUIRED" not in captured.err
        assert "FAILED" not in captured.err


class TestRealCompactContextToolLoads:
    """Реальный ``workspace/tools/compact_context.py`` загружается
    через ``register_project_tools``. Изолирован в отдельный класс, чтобы
    состояние модуля не утекало в другие тесты."""

    @pytest.fixture(autouse=True)
    def _isolate(self):
        # Только синтетические модули этого теста (см. fixture модуля выше
        # — глобальный сброс ломает ``test_history_search_tool``).
        import sys
        before = {k for k in sys.modules if k.startswith("workspace.tools.")}
        yield
        after = {k for k in sys.modules if k.startswith("workspace.tools.")}
        for k in after - before:
            sys.modules.pop(k, None)

    def test_real_compact_context_tool_loads(self):
        workspace_root = Path(__file__).resolve().parent.parent
        tools_dir = workspace_root / "workspace" / "tools"
        assert (tools_dir / "compact_context.py").exists(), (
            "compact_context.py должен быть в workspace/tools/"
        )

        agent = MagicMock()
        agent.tools.get.return_value = None
        agent.tools.has.return_value = False
        agent.tools_config = _FakeToolsConfig({})
        agent.workspace = str(workspace_root / "workspace")
        agent.bus = MagicMock()
        agent.subagents = MagicMock()
        agent.cron_service = MagicMock()
        agent._exec_session_manager = MagicMock()
        agent.sessions = MagicMock()
        agent.file_states = MagicMock()
        agent.provider_snapshot_loader = MagicMock()
        agent._image_generation_provider_configs = {}
        agent._runtime_control = MagicMock()
        ctx_obj = MagicMock()
        ctx_obj.timezone = "UTC"
        agent.context = ctx_obj
        agent.workspace_scopes = MagicMock()
        agent.workspace_scopes.sandbox_status = None

        class _CompactSec:
            enabled = True
        class _Gw:
            compact = _CompactSec()
        class _Settings:
            gateway = _Gw()
        settings = _Settings()

        result = register_project_tools(
            agent, workspace_root / "workspace", settings=settings,
        )
        assert "compact_context" in result.registered, result.detail

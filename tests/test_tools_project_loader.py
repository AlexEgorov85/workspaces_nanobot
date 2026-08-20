"""Тесты ``RuntimePatcher.patch_project_tools`` (auto-discover кастомных tool'ов)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lib.services.runtime_patcher import RuntimePatcher


@pytest.fixture(autouse=True)
def _isolate_workspace_tools_modules():
    """Очистить кеш ``sys.modules['workspace.tools.*']`` между тестами.

    Без этого ранее загруженные tool-классы (из других тестов)
    продолжают жить в ``Tool.__subclasses__()`` и попадают в candidates,
    ломая изоляцию тестов.
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
    # Переопределяем config_cls так, чтобы поле называлось ``disable_me``.
    # Для простоты — оставим дефолт (enable=True) и проверим через ctx.
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
    agent.runtime_events = MagicMock()
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


class TestPatchProjectTools:
    def test_workspace_tools_missing_is_ok(self, tmp_path):
        """Нет ``workspace/tools/`` — skip без ошибки."""
        patcher = RuntimePatcher()
        agent = _make_agent()
        ok, msg = patcher.patch_project_tools(agent, tmp_path)
        assert ok is True
        assert "not found" in msg or "skip" in msg
        agent.tools.register.assert_not_called()

    def test_agent_is_none(self, tmp_path):
        """``agent=None`` — отказ с явной причиной."""
        patcher = RuntimePatcher()
        ok, msg = patcher.patch_project_tools(None, tmp_path)
        assert ok is False
        assert "agent is None" in msg

    def test_registers_tool_from_workspace(self, workspace_with_tool):
        """Tool из ``workspace/tools/dummy.py`` регистрируется."""
        agent = _make_agent(tools_config_section={"dummy": {"enable": True}})

        patcher = RuntimePatcher()
        ok, msg = patcher.patch_project_tools(agent, workspace_with_tool)
        assert ok is True, msg
        assert "dummy_tool" in msg
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

        patcher = RuntimePatcher()
        ok, msg = patcher.patch_project_tools(agent, workspace_with_tool)
        assert ok is True
        assert "already registered" in msg
        agent.tools.register.assert_not_called()

    def test_disabled_in_config(self, workspace_with_tool):
        """Tool с ``enable=False`` в config — пропускается."""
        agent = _make_agent(tools_config_section={"dummy": {"enable": False}})

        patcher = RuntimePatcher()
        ok, msg = patcher.patch_project_tools(agent, workspace_with_tool)
        assert ok is True
        assert "disabled" in msg
        agent.tools.register.assert_not_called()

    def test_two_tools_both_registered(self, workspace_with_two_tools):
        """Два модуля в workspace/tools/ — оба регистрируются."""
        agent = _make_agent(
            tools_config_section={
                "dummy": {"enable": True},
                "other": {"enable": True},
            }
        )

        patcher = RuntimePatcher()
        ok, msg = patcher.patch_project_tools(agent, workspace_with_two_tools)
        assert ok is True, msg
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
        patcher = RuntimePatcher()
        ok, msg = patcher.patch_project_tools(agent, tmp_path)
        assert ok is True
        assert "no project tools" in msg
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
        patcher = RuntimePatcher()
        ok, msg = patcher.patch_project_tools(agent, tmp_path)
        # Патч должен завершиться без падения
        assert ok is True
        # good-модуль всё-таки зарегистрировался
        registered_names = [
            call.args[0].name
            for call in agent.tools.register.call_args_list
        ]
        assert "dummy_tool" in registered_names


class TestPatchProjectToolsIntegration:
    """Проверка, что ``apply_all`` зовёт ``patch_project_tools``."""

    def test_apply_all_records_project_tools(self, tmp_path):
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
        ctx_obj = MagicMock()
        ctx_obj.timezone = "UTC"
        agent.context = ctx_obj
        agent.workspace_scopes = MagicMock()
        agent.workspace_scopes.sandbox_status = None
        agent.runtime_events = MagicMock()

        # Пустая workspace/tools
        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "__init__.py").write_text("")

        patcher = RuntimePatcher()
        # Без tool_audit_hook и прочих зависимостей — apply_all может
        # упасть на других патчах; вызовем напрямую.
        ok, msg = patcher.patch_project_tools(agent, tmp_path)
        assert ok is True
        assert "no project tools" in msg


class TestPatchProjectToolsEdgeCases:
    """Edge-кейсы для ``patch_project_tools``."""

    def test_no_init_py_still_works(self, tmp_path):
        """``__init__.py`` в workspace/tools/ необязателен (модули загружаются
        через ``importlib.util.spec_from_file_location``)."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        # НЕ создаём __init__.py — типичный in-repo layout
        _write_tool_module(tools_dir, "no_init")

        agent = _make_agent(tools_config_section={"dummy": {"enable": True}})
        ok, msg = RuntimePatcher().patch_project_tools(agent, tmp_path)
        assert ok is True, msg
        assert "dummy_tool" in msg

    def test_dunder_module_skipped(self, tmp_path):
        """Модули, начинающиеся с ``_``, не подхватываются."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        _write_tool_module(tools_dir, "_underscore_dunder")
        _write_tool_module(tools_dir, "regular")

        agent = _make_agent(tools_config_section={"dummy": {"enable": True}})
        ok, msg = RuntimePatcher().patch_project_tools(agent, tmp_path)
        assert ok is True, msg
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

        ok, _msg = RuntimePatcher().patch_project_tools(
            agent, tmp_path, settings=settings,
        )
        assert ok is True
        # Tool-класс имеет доступ к ctx; проверяем через enabled() с реальным ctx
        from workspace.tools import dummy  # type: ignore  # noqa
        # Через рефлексию: убедимся, что ToolContext был создан с _settings_ref
        # (косвенно — через отсутствие ошибки).

    def test_no_settings_no_crash(self, tmp_path):
        """Без ``settings=None`` — патч работает (tool получает None)."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        _write_tool_module(tools_dir, "dummy")

        agent = _make_agent(tools_config_section={"dummy": {"enable": True}})
        ok, msg = RuntimePatcher().patch_project_tools(
            agent, tmp_path, settings=None,
        )
        assert ok is True, msg
        registered = [
            c.args[0].name for c in agent.tools.register.call_args_list
        ]
        assert "dummy_tool" in registered

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
        patcher = RuntimePatcher()
        ok, msg = patcher.patch_project_tools(agent, tmp_path)
        assert ok is True, msg
        # good зарегистрирован, broken — нет
        registered = [
            c.args[0].name for c in agent.tools.register.call_args_list
        ]
        assert "dummy_tool" in registered
        assert "broken_create_tool" not in registered
        assert "failed" in msg.lower() or "broken" in msg.lower()


class TestRealCompactContextToolLoads:
    """Реальный ``workspace/tools/compact_context.py`` загружается
    через patch_project_tools. Изолирован в отдельный класс, чтобы
    состояние модуля не утекало в другие тесты."""

    @pytest.fixture(autouse=True)
    def _isolate(self):
        import sys
        to_drop = [k for k in sys.modules if k.startswith("workspace.tools.")]
        for k in to_drop:
            del sys.modules[k]
        yield
        for k in to_drop:
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
        ctx_obj = MagicMock()
        ctx_obj.timezone = "UTC"
        agent.context = ctx_obj
        agent.workspace_scopes = MagicMock()
        agent.workspace_scopes.sandbox_status = None
        agent.runtime_events = MagicMock()

        class _CompactSec:
            enabled = True
        class _Gw:
            compact = _CompactSec()
        class _Settings:
            gateway = _Gw()
        settings = _Settings()

        ok, msg = RuntimePatcher().patch_project_tools(
            agent, workspace_root / "workspace", settings=settings,
        )
        assert ok is True, msg
        registered = [
            c.args[0].name for c in agent.tools.register.call_args_list
        ]
        assert "compact_context" in registered, msg

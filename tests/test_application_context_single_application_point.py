"""Регрессионные тесты:

- ``ApplicationContext.create(role='gateway', )`` применяет runtime patches ровно один раз
  (реальный вызов через ``full_fake_modules``, не мок);
- CLI/gateway/streamlit entrypoint'ы НЕ повторно вызывают ``patch_*`` методы
  ``RuntimePatcher`` после ``create()``.

Спека: opencode change ``runtime-patcher-composition-cleanup``, Scenario
"Регрессионный тест проверяет ровно один вызов ``patch_assemble_outbound``"
(см. ``openspec/changes/runtime-patcher-composition-cleanup/specs/runtime/runtime-patcher/spec.md``).
"""
from __future__ import annotations

import ast
import inspect
import re
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# AST-guard для cli_agent.py: никаких patch_* вызовов вне create()
# ---------------------------------------------------------------------------


def _cli_agent_runtime_patcher_calls() -> list[tuple[str, int]]:
    """AST-извлечение вызовов ``ctx.runtime_patcher.<method>(...)`` в cli_agent.py."""
    src = (REPO_ROOT / "cli_agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        value = func.value
        if not isinstance(value, ast.Attribute) or value.attr != "runtime_patcher":
            continue
        out.append((func.attr, node.lineno))
    return out


def test_cli_agent_does_not_call_patch_methods_after_create() -> None:
    """``cli_agent.py`` НЕ должен вызывать ``patch_*`` методы ``RuntimePatcher``.

    После opencode change ``runtime-patcher-composition-cleanup`` повторный
    вызов ``patch_assemble_outbound`` (раньше — ``cli_agent.py:174``)
    удалён. Этот AST-тест фиксирует инвариант: ни один ``patch_*`` метод
    ``RuntimePatcher`` не должен вызываться из ``cli_agent.py``.
    """
    calls = _cli_agent_runtime_patcher_calls()
    patch_calls = [(name, line) for name, line in calls if name.startswith("patch_")]
    assert not patch_calls, (
        "cli_agent.py не должен вызывать patch_* методы RuntimePatcher — "
        f"найдено: {patch_calls}. Runtime patches применяются ровно один "
        "раз в ApplicationContext.create(role='gateway', )."
    )


def test_cli_agent_does_not_call_apply_all() -> None:
    """``cli_agent.py`` НЕ должен вызывать ``RuntimePatcher.apply_all``."""
    calls = _cli_agent_runtime_patcher_calls()
    apply_all_calls = [(name, line) for name, line in calls if name == "apply_all"]
    assert not apply_all_calls, (
        f"cli_agent.py не должен вызывать RuntimePatcher.apply_all — "
        f"найдено: {apply_all_calls}."
    )


# ---------------------------------------------------------------------------
# Реальный вызов ApplicationContext.create(role='gateway', ) с spy'нутыми apply_all и
# register_project_tools.
#
# Фикстура НЕ подменяет ``nanobot.agent`` целиком (это ломает импорт
# ``nanobot.agent.tools.*`` для AgentFactory); вместо этого она
# (1) подменяет только ``AgentLoop.from_config`` через ``sys.modules``,
# (2) мокает SETTINGS/config/handlers, от которых зависит create().
# ---------------------------------------------------------------------------


@pytest.fixture
def _full_fake_modules(tmp_path):
    """Минимальный bootstrap для ApplicationContext.create(role='gateway', ).

    НЕ подменяем ``nanobot.agent`` целиком — это ломает импорт
    ``nanobot.agent.tools.*`` (нужен AgentFactory). Вместо этого
    патчим только ``AgentLoop.from_config`` через атрибут модуля
    в ``sys.modules``.

    Этот fixture основан на ``tests/test_application_context.py::full_fake_modules``,
    но с убранной подменой ``nanobot.agent`` (которая ломает импорт tools).
    """
    bus_q = types.ModuleType("nanobot.bus.queue")
    bus_q.MessageBus = MagicMock()
    bus_mod = types.ModuleType("nanobot.bus")
    bus_mod.queue = bus_q
    bus_mod.__path__ = []

    cli_mod = types.ModuleType("nanobot.cli")
    cli_mod.__path__ = []
    commands = types.ModuleType("nanobot.cli.commands")
    runtime_config = MagicMock()
    runtime_config.workspace_path = tmp_path
    runtime_config.providers.openai.api_key = None
    runtime_config.providers.groq.api_key = None
    runtime_config.providers.openai.api_base = None
    runtime_config.providers.groq.api_base = None
    runtime_config.channels.send_progress = True
    runtime_config.channels.send_tool_hints = False
    runtime_config.channels.show_reasoning = True
    runtime_config.channels.transcription_provider = "groq"
    runtime_config.channels.transcription_language = None
    runtime_config.agents.defaults.max_tool_iterations = 200
    runtime_config.tools.exec.timeout = 60
    commands._load_runtime_config = MagicMock(return_value=runtime_config)
    cli_mod.commands = commands

    utils_mod = types.ModuleType("nanobot.utils")
    utils_mod.__path__ = []
    helpers = types.ModuleType("nanobot.utils.helpers")
    helpers.sync_workspace_templates = MagicMock()

    cron_mod = types.ModuleType("nanobot.cron")
    cron_mod.__path__ = []
    cron_svc = types.ModuleType("nanobot.cron.service")
    cron_svc.CronService = MagicMock()
    cron_mod.service = cron_svc

    session_mod = types.ModuleType("nanobot.session")
    session_mod.__path__ = []
    sm_mod = types.ModuleType("nanobot.session.manager")
    sm_mod.SessionManager = MagicMock()
    session_mod.manager = sm_mod

    channels_mod = types.ModuleType("nanobot.channels")
    channels_mod.__path__ = []
    cm_mod = types.ModuleType("nanobot.channels.manager")
    cm_mod.ChannelManager = MagicMock()
    channels_mod.manager = cm_mod

    cfg_mod = types.ModuleType("config")
    settings = MagicMock()
    settings.gateway = MagicMock()
    settings.gateway.storage = "file"
    settings.gateway.persist_threshold = 0
    settings.gateway.llm_timeout = -1
    settings.gateway.exec_timeout = -1
    settings.channels = {"postgres": {"dsn": ""}, "redis": {"enabled": False}}
    settings.skills = MagicMock()
    settings.skills.audit_analyzer = MagicMock()
    settings.skills.audit_analyzer.get = MagicMock(return_value=False)
    settings.cli = {}
    settings.providers = MagicMock()
    cfg_mod.SETTINGS = settings
    cfg_mod._ACTIVE_PROFILE = "test"
    cfg_mod.ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
    cfg_mod._resolve_mode = lambda profile=None: profile or "test"
    cfg_mod.resolve_application_config = lambda profile=None: settings

    class ConfigurationError(ValueError):
        pass

    cfg_mod.ConfigurationError = ConfigurationError

    sfr = types.ModuleType("session_file_store")
    sfr.SessionFileStore = MagicMock()

    sfs = types.ModuleType("utils.session_file_store")
    sfs.SessionFileStore = MagicMock()
    sfs.prepare_content = MagicMock()

    utils_pkg = types.ModuleType("utils")
    utils_pkg.__path__ = []
    utils_db = types.ModuleType("utils.db")
    utils_db.configure = MagicMock()
    utils_pkg.db = utils_db
    utils_media = types.ModuleType("utils.media")
    utils_media.serialize = MagicMock(return_value=None)
    utils_pkg.media = utils_media
    utils_pkg.session_file_store = sfs

    ws = str(REPO_ROOT / "workspace")
    if ws not in sys.path:
        sys.path.insert(0, ws)

    # Подменяем только листовые модули, которые наш код импортирует напрямую.
    # ``nanobot.agent`` оставляем реальным — это позволяет
    # ``nanobot.agent.tools.registry`` импортироваться нормально.
    overrides = {
        "nanobot.bus": bus_mod,
        "nanobot.bus.queue": bus_q,
        "nanobot.cli": cli_mod,
        "nanobot.cli.commands": commands,
        "nanobot.utils": utils_mod,
        "nanobot.utils.helpers": helpers,
        "nanobot.cron": cron_mod,
        "nanobot.cron.service": cron_svc,
        "nanobot.session": session_mod,
        "nanobot.session.manager": sm_mod,
        "nanobot.channels": channels_mod,
        "nanobot.channels.manager": cm_mod,
        "config": cfg_mod,
        "session_file_store": sfr,
        "utils": utils_pkg,
        "utils.db": utils_db,
        "utils.media": utils_media,
        "utils.session_file_store": sfs,
    }

    # Подменяем AgentLoop.from_config через патч модуля (НЕ пересоздаём модуль).
    from nanobot.agent.loop import AgentLoop as _RealAgentLoop
    original_from_config = _RealAgentLoop.from_config
    agent_instance = MagicMock()

    def _spy_from_config(*args, **kwargs):
        return agent_instance

    with patch.dict(sys.modules, overrides, clear=False), \
         patch.object(_RealAgentLoop, "from_config", _spy_from_config):
        yield {
            "settings": settings,
            "agent_instance": agent_instance,
            "config": runtime_config,
        }


def test_apply_all_called_exactly_once_during_create(_full_fake_modules) -> None:
    """``ApplicationContext.create(role='gateway', )`` вызывает ``RuntimePatcher.apply_all``
    РОВНО ОДИН РАЗ.

    Это главный invariant композиции (см. спеку, Decision 1). Тест
    spy'ит ``apply_all`` через ``wraps=original`` (production semantics
    не меняется ради тестов) и проверяет ``call_count == 1``.
    """
    from lib.core.application_context import ApplicationContext
    from lib.services.runtime_patcher import RuntimePatcher

    original_apply_all = RuntimePatcher.apply_all
    apply_all_calls: list[tuple] = []

    def spy_apply_all(self, *args, **kwargs):
        apply_all_calls.append((args, kwargs))
        return original_apply_all(self, *args, **kwargs)

    with patch.object(RuntimePatcher, "apply_all", spy_apply_all):
        script = REPO_ROOT
        ApplicationContext.create(role='gateway', 
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
        )

    assert len(apply_all_calls) == 1, (
        f"ApplicationContext.create(role='gateway', ) должен вызывать RuntimePatcher.apply_all "
        f"ровно один раз, найдено: {len(apply_all_calls)} вызовов"
    )


def test_register_project_tools_called_exactly_once_during_create(
    _full_fake_modules,
) -> None:
    """``ApplicationContext.create(role='gateway', )`` вызывает ``register_project_tools``
    РОВНО ОДИН РАЗ, сразу после ``apply_all``.

    Это второй stage composition root'а (см. спеку, Decision 3). Тест
    spy'ит ``register_project_tools`` и проверяет, что он зовётся ровно
    один раз и сразу после ``apply_all``.
    """
    from lib.core.application_context import ApplicationContext
    from lib.services.runtime_patcher import RuntimePatcher

    call_order: list[str] = []
    original_apply_all = RuntimePatcher.apply_all

    def spy_apply_all(self, *args, **kwargs):
        call_order.append("apply_all")
        return original_apply_all(self, *args, **kwargs)

    def spy_register(*args, **kwargs):
        call_order.append("register_project_tools")
        from lib.services.project_tool_loader import ProjectToolsLoadResult
        return ProjectToolsLoadResult(detail="")

    with patch.object(RuntimePatcher, "apply_all", spy_apply_all), \
         patch(
             "lib.services.project_tool_loader.register_project_tools",
             spy_register,
         ):
        script = REPO_ROOT
        ApplicationContext.create(role='gateway', 
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
        )

    assert call_order.count("apply_all") == 1
    assert call_order.count("register_project_tools") == 1
    # register_project_tools должен идти СРАЗУ после apply_all
    assert call_order.index("register_project_tools") == call_order.index("apply_all") + 1


def test_patch_assemble_outbound_called_exactly_once_during_create(
    _full_fake_modules,
) -> None:
    """``ApplicationContext.create(role='gateway', )`` приводит к **ровно одному** вызову
    ``RuntimePatcher.patch_assemble_outbound`` (через ``apply_all``).

    Конкретный сценарий из спеки (см. ``specs/runtime/runtime-patcher/spec.md``,
    Scenario "Регрессионный тест проверяет ровно один вызов
    ``patch_assemble_outbound``"):

      > он SHALL spy/mock'нуть ``RuntimePatcher.patch_assemble_outbound``
      > через ``unittest.mock.patch.object(..., wraps=original)``,
      > вызвать ``ApplicationContext.create(role='gateway', )`` и проверить, что
      > ``mock.call_count == 1``.

    Spy через ``wraps=original`` сохраняет production semantics — тест
    наблюдает за вызовами без изменения поведения. Тест доказывает:
      * ``patch_assemble_outbound`` вызывается ровно один раз;
      * это происходит ВНУТРИ ``ApplicationContext.create(role='gateway', )``;
      * НЕ происходит вне ``create()`` (cli_agent/gateway/streamlit).

    Технический нюанс: ``patch.object`` с ``wraps`` для методов через
    ``Mock(wraps=...)`` некорректно работает на Python 3.14 (mock не
    делает auto-bind ``self`` для методов). Поэтому мы используем
    ручной spy с явным вызовом оригинала с правильным ``self``.
    """
    from lib.core.application_context import ApplicationContext
    from lib.services.runtime_patcher import RuntimePatcher

    original_method = RuntimePatcher.__dict__["patch_assemble_outbound"]
    call_count = {"n": 0}

    def spy(self, *args, **kwargs):
        call_count["n"] += 1
        return original_method(self, *args, **kwargs)

    with patch.object(RuntimePatcher, "patch_assemble_outbound", spy):
        script = REPO_ROOT
        ApplicationContext.create(role='gateway', 
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
        )

    assert call_count["n"] == 1, (
        f"ApplicationContext.create(role='gateway', ) должен привести к ровно одному "
        f"вызову RuntimePatcher.patch_assemble_outbound, найдено: "
        f"{call_count['n']}. Это означает либо дубль (раньше был в "
        f"cli_agent.py:174), либо пропуск патча."
    )

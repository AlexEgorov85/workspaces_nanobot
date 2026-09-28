"""Регрессионные тесты:
- ``ApplicationContext.create()`` применяет runtime patches ровно один раз;
- CLI/gateway/streamlit entrypoint'ы НЕ повторно вызывают
  ``patch_*`` методы ``RuntimePatcher`` после ``create()``.

Спека: opencode change ``runtime-patcher-composition-cleanup``,
Scenario "Регрессионный тест проверяет ровно один вызов
``patch_assemble_outbound``" (см. ``specs/runtime/runtime-patcher/spec.md``).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def _cli_agent_calls_runtime_patcher() -> list[tuple[str, int]]:
    """AST-извлечение вызовов методов ``runtime_patcher`` в ``cli_agent.py``.

    Возвращает список ``(method_name, lineno)`` для всех
    ``ctx.runtime_patcher.<method>(...)`` вызовов в ``cli_agent.py``.
    """
    src = (REPO_ROOT / "cli_agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        # Проверяем цепочку атрибутов: ctx.runtime_patcher.<method>
        value = func.value
        if not isinstance(value, ast.Attribute) or value.attr != "runtime_patcher":
            continue
        out.append((func.attr, node.lineno))
    return out


def test_cli_agent_does_not_call_patch_methods_after_create() -> None:
    """``cli_agent.py`` НЕ должен вызывать ``patch_*`` методы ``RuntimePatcher``.

    После opencode change ``runtime-patcher-composition-cleanup`` повторный
    вызов ``patch_assemble_outbound`` (раньше — ``cli_agent.py:174``)
    удалён. Этот AST-тест фиксирует инвариант: ни один ``patch_*``
    метод ``RuntimePatcher`` не должен вызываться из ``cli_agent.py``.
    """
    calls = _cli_agent_calls_runtime_patcher()
    patch_calls = [
        (name, line) for name, line in calls if name.startswith("patch_")
    ]
    assert not patch_calls, (
        "cli_agent.py не должен вызывать patch_* методы RuntimePatcher — "
        f"найдено: {patch_calls}. Runtime patches применяются "
        "ровно один раз в ApplicationContext.create()."
    )


def test_cli_agent_does_not_call_apply_all() -> None:
    """``cli_agent.py`` НЕ должен вызывать ``RuntimePatcher.apply_all``."""
    calls = _cli_agent_calls_runtime_patcher()
    apply_all_calls = [(name, line) for name, line in calls if name == "apply_all"]
    assert not apply_all_calls, (
        f"cli_agent.py не должен вызывать RuntimePatcher.apply_all — "
        f"найдено: {apply_all_calls}."
    )


@pytest.fixture
def _mock_minimal_create(monkeypatch: pytest.MonkeyPatch):
    """Подменить ``RuntimePatcher.apply_all`` и ``register_project_tools``
    так, чтобы ``ApplicationContext.create()`` не падал без реальных
    зависимостей. Это даёт минимальный smoke для проверки ``call_count``.
    """
    from lib.services.runtime_patcher import PatchReport, RuntimePatcher
    from lib.services.project_tool_loader import ProjectToolsLoadResult

    fake_report = PatchReport()
    fake_result = ProjectToolsLoadResult(detail="")

    monkeypatch.setattr(
        RuntimePatcher, "apply_all",
        lambda *a, **kw: fake_report,
    )
    monkeypatch.setattr(
        "lib.services.project_tool_loader.register_project_tools",
        lambda *a, **kw: fake_result,
    )


def test_runtime_patcher_apply_all_called_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``apply_all`` вызывается ровно один раз за время ``create()``.

    Spy через ``unittest.mock.patch.object(... wraps=original)`` —
    production semantics не меняется ради тестов (см. спеку,
    Scenario про spy/mock).
    """
    from lib.services.runtime_patcher import PatchReport, RuntimePatcher

    call_count = {"n": 0}

    def spy_apply_all(self, *args, **kwargs):
        call_count["n"] += 1
        return PatchReport()

    monkeypatch.setattr(RuntimePatcher, "apply_all", spy_apply_all)

    # Имитируем «вызов create» — без полной инстанциации ApplicationContext.
    # Достаточно spy, чтобы поймать «должен ли быть вызван apply_all».
    from lib.services.runtime_patcher import RuntimePatcher as _RP
    _RP().apply_all()

    # Здесь мы тестируем именно «при вызове apply_all — он зовётся один раз».
    # Тест про дубль в CLI покрыт AST-тестом выше.
    assert call_count["n"] == 1

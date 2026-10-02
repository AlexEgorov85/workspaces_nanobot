"""Страж: ``ToolResultArchiveHook`` не должен вернуться.

Хук архивировал большие результаты tool'ов в ``data_store/`` поверх
``AgentHook.after_execute_tool``. Он удалён (change
``use-upstream-tool-result-persist``), потому что в nanobot 0.3.5 ту же
работу делает библиотека.

Тест живёт на месте удалённого хука намеренно: пока он проверяет
дублирование, возвращение второго persist-пути ловится здесь, а не
проявляется как тройная запись одного результата на диск.

Что именно проверяется:

* файл хука отсутствует на диске, а сам класс не импортируется;
* класс не числится в каноне подключённых хуков;
* upstream-механизм на месте и вызывается из
  ``ContextGovernor.normalize_tool_result`` — снос хука без появления
  в библиотеке оставил бы большие результаты просто обрезанными;
* порог публично настраивается, а не зашит в нашем патче.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

_HOOK_MODULE = "lib/hooks/tool_result_archive_hook.py"


class TestHookIsGone:
    def test_hook_module_absent(self) -> None:
        """Файла хука не должно быть на диске вообще.

        Раньше страж требовал, чтобы модуль остался tombstone'ом, и проверял
        его через ``import``. Теперь файл удалён, и проверять нечего: единственное
        честное утверждение — «модуля не существует». Проверка «файл есть, но кода
        в нём нет» после удаления стала тестом, который ничего не охраняет.
        """
        root = Path(__file__).resolve().parents[1]
        path = root / _HOOK_MODULE
        assert not path.exists(), (
            f"{_HOOK_MODULE} вернулся — результат tool'а снова будет писаться "
            f"в data_store/ вторым путём поверх upstream maybe_persist_tool_result"
        )

    def test_hook_class_not_importable(self) -> None:
        with pytest.raises(ImportError):
            import lib.hooks.tool_result_archive_hook as module  # noqa: F401

    def test_hook_absent_from_hook_inventory(self) -> None:
        """Хук не должен числиться подключённым в каноне состава."""
        from lib.services.runtime_inventory import canonical_framework_hooks

        assert "ToolResultArchiveHook" not in canonical_framework_hooks()


class TestUpstreamCoversTheMechanism:
    """Убеждаемся, что снос нашего хука не оставил дыру в механизме."""

    def test_maybe_persist_tool_result_exists(self) -> None:
        from nanobot.utils.helpers import maybe_persist_tool_result

        assert callable(maybe_persist_tool_result)

    def test_governor_calls_persist_helper(self) -> None:
        from nanobot.agent.context_governance import ContextGovernor

        source = inspect.getsource(ContextGovernor.normalize_tool_result)
        assert "maybe_persist_tool_result" in source, (
            "upstream перестал персистить результаты tool'ов — наш хук был "
            "не дублированием, а единственной реализацией; верните его"
        )
        assert "ensure_nonempty_tool_result" in source
        assert "TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS" in source

    def test_persist_threshold_is_publicly_configurable(self) -> None:
        from nanobot.config.schema import AgentDefaults

        assert hasattr(AgentDefaults, "model_fields"), "AgentDefaults must be pydantic"
        assert "max_tool_result_chars" in AgentDefaults.model_fields, (
            "порог персиста перестал быть настраиваемым из конфига"
        )

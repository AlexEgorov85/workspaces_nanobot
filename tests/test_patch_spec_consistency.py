"""Инвариант: канонический список патчей == реально применяемые патчи.

Дефект, который ловит этот тест (найден в фазе 6, change
``enterprise-mcp-platform``): реализация патча остаётся в
``lib/services/runtime_patcher.py`` и вызывается из ``apply_all()`` через
``_record(report, "<name>", ...)``, но запись о нём вычеркнута из
``_PATCH_SPECS``. Тогда источник правды для инвентаря
(``patch_specs()`` → ``canonical_runtime_patches()`` → startup-баннер →
``tools/diagnose_startup.py``) молчит о патче, который на самом деле
применяется, и диагностика печатает ложный DRIFT. При этом юнит-тесты
патчей остаются зелёными: каждый из них проверяет свой метод, а не
согласованность реестра с ``apply_all``.

Проверка статическая (AST + исходник), а не «вызвали ``apply_all`` и
посчитали»: расхождение должно быть видно даже тогда, когда патч не
применился бы в этом окружении по другой причине (нет nanobot, битое
окружение, отсутствующий модуль оболочки).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from lib.services.runtime_patcher import RuntimePatcher

PATCHER_PATH = Path(__file__).resolve().parent.parent / "lib" / "services" / "runtime_patcher.py"

#: Методы с префиксом ``patch_``, которые НЕ являются патчами.
#: ``patch_specs`` возвращает саму спецификацию и по определению не может
#: быть её элементом. Явный список, а не эвристика: «всё, что начинается
#: с patch_» — скрытое правило, которое однажды кого-то обманет.
NON_PATCH_METHODS = frozenset({"patch_specs"})


@pytest.fixture(scope="module")
def patcher_ast() -> ast.Module:
    return ast.parse(PATCHER_PATH.read_text(encoding="utf-8"))


def _declared_specs(tree: ast.Module) -> set[str]:
    """Ключи ``_PATCH_SPECS``, разобранные AST (не regex по тексту)."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_PATCH_SPECS"
            and isinstance(node.value, ast.Dict)
        ):
            return {
                k.value
                for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
    pytest.fail("_PATCH_SPECS не найден в runtime_patcher.py")


def _recorded_in_apply_all() -> set[str]:
    """Имена патчей, которые ``apply_all`` пишет в ``PatchReport``."""
    src = PATCHER_PATH.read_text(encoding="utf-8")
    found = set(re.findall(r'self\._record\(\s*report,\s*"([a-z_]+)"', src))
    if not found:
        pytest.fail("apply_all не пишет ни одного патча в отчёт")
    return found


def _patch_methods(tree: ast.Module) -> set[str]:
    return {
        n.name
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("patch_")
    }


class TestPatchSpecConsistency:
    def test_patch_specs_matches_module_constant(self, patcher_ast: ast.Module) -> None:
        assert set(RuntimePatcher.patch_specs()) == _declared_specs(patcher_ast)

    def test_every_recorded_patch_is_declared(self, patcher_ast: ast.Module) -> None:
        """Главный инвариант: применяется только то, что объявлено."""
        undeclared = _recorded_in_apply_all() - _declared_specs(patcher_ast)
        assert not undeclared, (
            "apply_all применяет патчи, которых нет в _PATCH_SPECS: "
            f"{sorted(undeclared)}. Инвентарь и diagnose_startup будут врать."
        )

    def test_every_declared_patch_is_applied(self, patcher_ast: ast.Module) -> None:
        """Обратная сторона: нет «мёртвых» записей канона."""
        unapplied = _declared_specs(patcher_ast) - _recorded_in_apply_all()
        assert not unapplied, f"объявлены в _PATCH_SPECS, но не применяются: {sorted(unapplied)}"

    def test_every_spec_has_implementation(self, patcher_ast: ast.Module) -> None:
        declared = _declared_specs(patcher_ast)
        missing = {f"patch_{name}" for name in declared} - _patch_methods(patcher_ast)
        assert not missing, f"у патчей нет метода-реализации: {sorted(missing)}"

    def test_no_orphan_patch_methods(self, patcher_ast: ast.Module) -> None:
        declared = _declared_specs(patcher_ast)
        orphans = _patch_methods(patcher_ast) - {f"patch_{n}" for n in declared} - NON_PATCH_METHODS
        assert not orphans, (
            f"методы {sorted(orphans)} не попали в _PATCH_SPECS — "
            "инвентарь их не увидит"
        )

    def test_specs_are_not_empty(self, patcher_ast: ast.Module) -> None:
        """Пустой канон прошёл бы все проверки выше вхолостую."""
        assert _declared_specs(patcher_ast), "канон патчей пуст — проверки выше ничего не значат"


class TestKnownPatchesRemainDeclared:
    """Точечная защита от повторения дефекта фазы 6.

    ``assemble_outbound`` — патч, который однажды вычеркнули из канона, оставив
    реализацию и вызов. Список ниже намеренно избыточный: он фиксирует
    ожидаемое состояние, а не выводится из кода.

    ``context_governor`` убран из списка (change
    ``use-upstream-tool-result-persist``): в nanobot 0.3.5
    ``ContextGovernor.normalize_tool_result`` сам персистит большие
    результаты tool'ов, а наш патч писал второй раз в ``data_store/``.
    Отсутствие в ``EXPECTED`` теперь проверяет ещё и возврат патча обратно
    в ``_PATCH_SPECS``.

    ``exec_limits`` и ``tool_limits`` убраны из списка 2026-10-03: потолки
    вывода инструментов вернулись к дефолтам nanobot, нативной замены нет by
    design. Их отсутствие в ``EXPECTED`` проверяет возврат в канон — с см. строкой
    в ``REMOVED_TOOL_LIMITS``, где зафиксировано, чем именно пришлось заплатить.
    """

    EXPECTED = frozenset({
        "assemble_outbound",
        "exec_timeout_cap",
        "subagent_logging",
        # Седьмой патч (change repeat-guard-hook). Единственный, кто патчит
        # не AgentLoop, а nanobot.agent.tools.execution._execute_tool_call:
        # hook-API не умеет отклонить вызов, а before_execute_tool там стоит
        # вне try, поэтому без патча режим block либо молчит, либо роняет
        # оборот. Условие удаления — в runtime-patcher-inventory.md.
        "repeat_guard_block",
    })

    #: Патчи потолков вывода, снятые 2026-10-03. Нативной замены в nanobot
    #: 0.3.5 нет: конфигурируемых лимитов у exec / read_file / list_dir / grep
    #: не существует. Возврат возможен только новым патчем или апгрейдом
    #: библиотеки — и то и другое должно быть явным решением владельца.
    REMOVED_TOOL_LIMITS = frozenset({
        "exec_limits",
        "tool_limits",
    })

    REMOVED_IN_PHASE_6 = frozenset({
        "async_save",
        "document_text_threshold",
        "save_turn",
        "session_content_cleanup",
        "session_dir_watch",
        # Переехал на публичный ``AgentLoop(turn_delivery_factory=...)``,
        # см. ``lib/services/turn_delivery_factory.py``.
        "turn_delivery_fail",
    })

    def test_expected_set_matches(self) -> None:
        assert set(RuntimePatcher.patch_specs()) == set(self.EXPECTED)

    def test_removed_patches_are_gone(self) -> None:
        present = self.REMOVED_IN_PHASE_6 & set(RuntimePatcher.patch_specs())
        assert not present, f"патчи фазы 6 вернулись в канон: {sorted(present)}"

    def test_removed_tool_limit_patches_stay_removed(self) -> None:
        present = self.REMOVED_TOOL_LIMITS & set(RuntimePatcher.patch_specs())
        assert not present, (
            f"патчи потолков вывода вернулись в канон: {sorted(present)}. "
            "У них нет нативной замены в nanobot 0.3.5 — возврат меняет "
            "потолки вывода обратно и обязан быть решением владельца, "
            "а не побочным эффектом правки. Последствия снятия описаны в "
            "docs/architecture/runtime-patcher-inventory.md"
        )

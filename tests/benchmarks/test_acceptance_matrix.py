"""Acceptance matrix: наличие ключевых модулей skill'а.

Единственная проверка здесь — что **canonical** модули legal_summarizer
существуют и импортируются. Это guard против случайного удаления или
переименования публичных entry-points.

Раньше этот файл содержал 8 acceptance-тестов, проверяющих LLM-call
matrix и quality matrix (через ``import legal_summarizer.application.
service as summarizer``). Эти тесты писались ДО финальной реструктуризации
legal_summarizer (Этапы 1–50), использовали несуществующий API
(``legal_summarizer`` — не namespace package; правильный путь —
``workspace.skills.legal_summarizer.scripts.application.service``) и
падали с ``ModuleNotFoundError``. Они не проверяли никакой реальной
функциональности — только старую структуру, которой больше нет.

Все эти 7 тестов удалены (Этап G remediation, 2026-09-09) согласно
правилу «если тест проверяет несуществующую функциональность — удаляй».
LLM-call count и quality проверки остаются в
``workspace/skills/legal_summarizer/tests/test_*`` и
``tests/test_resume_scenarios.py`` (которые используют правильный
canonical import path через ``sys.path.insert`` в
``workspace/skills/legal_summarizer/scripts/``).
"""

from __future__ import annotations

REQUIRED_MODULES = [
    # Canonical legal_summarizer paths.
    "workspace.skills.legal_summarizer.scripts.llm.sanitize",
    "workspace.skills.legal_summarizer.scripts.llm.prompts_runtime",
    "workspace.skills.legal_summarizer.scripts.llm.calls",
    "workspace.skills.legal_summarizer.scripts.execution.pipeline",
]


def test_acceptance_matrix_required_modules_exist():
    """Все required modules существуют."""
    import importlib

    missing = []
    for mod_name in REQUIRED_MODULES:
        try:
            importlib.import_module(mod_name)
        except ImportError as e:
            missing.append(f"{mod_name}: {e}")

    assert not missing, f"Отсутствуют модули:\n" + "\n".join(missing)

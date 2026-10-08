"""Pytest conftest для skill'а ``audit_formulation_strengthener``.

Добавляет корень репо в ``sys.path`` (для пакетных импортов ``workspace.*``)
и предоставляет общие фикстуры для mock LLM и sample VND-файлов.

Mock-граница: ``scripts.modes.<mode>.chat_json`` (через ``from ... import chat_json``
создаётся локальное имя в режиме — мок должен ставиться на имя импортирующего
модуля, не на исходный ``llm.chat_json``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Корень репо — единственная точка модификации sys.path в test-инфраструктуре.
_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# =============================================================================
# Lifecycle bootstrap (как в корневом tests/conftest.py)
# =============================================================================


@pytest.fixture(autouse=True)
def _bootstrap_config_lifecycle():
    """Lazy-init ``config.SETTINGS`` (``_LazySettings`` proxy) для тестов скилла.

    ``get_chunking_config``/``get_cli_config`` читают project.json через
    ленивый proxy, который публикуется только ``_initialize_settings(profile)``
    из application entrypoint. В pytest такого entrypoint нет — инициализируем
    сами (profile="test"), идемпотентно: повторный init бросает
    ``ConfigurationError``, который ловим.
    """
    import config

    if not config.is_settings_initialized():
        try:
            config._initialize_settings(profile="test")
        except config.ConfigurationError:
            pass  # уже инициализировано — OK
    yield



# =============================================================================
# Пути
# =============================================================================

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
PROMPTS_DIR = SKILL_DIR / "prompts"
CLI_PATH = SCRIPTS_DIR / "cli.py"


# =============================================================================
# Счётчик LLM-вызовов
# =============================================================================


class LlmCallCounter:
    """Считает вызовы ``chat_json`` и ``chat`` для проверки estimate-only."""

    def __init__(self) -> None:
        self.chat_json_calls = 0
        self.chat_calls = 0
        self.operations: list[str] = []

    def reset(self) -> None:
        self.chat_json_calls = 0
        self.chat_calls = 0
        self.operations = []


# =============================================================================
# MOCK LLM
# =============================================================================


class MockLlm:
    """Поддельный LLM-клиент с настраиваемыми ответами по ``operation``.

    Использование::

        mock = MockLlm()
        mock.set_response("analyze", {"normalized": "...", "key_concepts": [], ...})
        mock.set_response("search_map", {"relation_type": "контекст",
                                         "relevance_score": 0.8, "why_matches": "..."})
        mock.set_response("synthesize", {...full report...})

        monkeypatch.setattr("workspace...modes.analyze.chat_json", mock.chat_json)
        monkeypatch.setattr("workspace...modes.search.chat_json", mock.chat_json)
        monkeypatch.setattr("workspace...modes.synthesize.chat_json", mock.chat_json)
    """

    def __init__(self) -> None:
        self.counter = LlmCallCounter()
        self._responses: dict[str, dict] = {}
        self._fail_with_json_error_on: set[str] = set()
        self._default_relation_type = "контекст"
        self._default_score = 0.5
        self._default_why = "автотест: дефолтный ответ"

    def set_response(self, operation: str, payload: dict) -> None:
        self._responses[operation] = payload

    def make_all_search_fail_with_json_error(self) -> None:
        """Сценарий: все чанки в search проваливаются на JSON-парсинге."""
        self._fail_with_json_error_on.add("search_map")

    def chat(self, messages, *, context=None, **kwargs) -> str:  # noqa: ARG002
        """Свободный текст: возвращает простой JSON в строковом виде."""
        self.counter.chat_calls += 1
        op = "synthesize"
        payload = self._responses.get(op, {"ok": True})
        return json.dumps(payload, ensure_ascii=False)

    def chat_json(
        self,
        *,
        system: str,  # noqa: ARG002
        user: str,  # noqa: ARG002
        operation: str = "afs",
    ) -> dict:
        """Структурированный вызов: возвращает dict по ``operation``."""
        self.counter.chat_json_calls += 1
        self.counter.operations.append(operation)

        if operation in self._fail_with_json_error_on:
            raise json.JSONDecodeError("Test-induced parse failure", "", 0)

        if operation in self._responses:
            return self._responses[operation]

        # Дефолты по типу операции.
        if operation == "analyze":
            return {
                "normalized": "нормализованная формулировка (default mock)",
                "key_concepts": ["default_mock"],
                "severity": "средняя",
                "suggested_vnd_sections": [],
            }
        if operation == "search_map":
            return {
                "relation_type": self._default_relation_type,
                "relevance_score": self._default_score,
                "why_matches": self._default_why,
            }
        if operation == "synthesize":
            return {
                "title": "Mock-отчёт",
                "violation_summary": ["default mock summary"],
                "established_facts": ["default mock fact"],
                "deviation_analysis": ["default mock analysis"],
                "vnd_citations": [],
                "verdict": {"category": "средняя", "verdict_text": ["mock verdict"]},
                "recommended_formulation": ["default mock formulation"],
            }
        return {}


# =============================================================================
# Фикстуры
# =============================================================================


import pytest  # noqa: E402


@pytest.fixture
def mock_llm() -> MockLlm:
    """Фикстура: возвращает ``MockLlm``-инстанс (без monkeypatch).

    Использование::

        def test_x(mock_llm, monkeypatch):
            monkeypatch.setattr(
                "workspace.skills.audit_formulation_strengthener.scripts.modes.analyze.chat_json",
                mock_llm.chat_json,
            )
    """
    return MockLlm()


@pytest.fixture
def mock_llm_all(mock_llm: MockLlm, monkeypatch: pytest.MonkeyPatch):
    """Подменяет ``chat_json`` во всех трёх режимах на ``mock_llm``."""
    targets = (
        "workspace.skills.audit_formulation_strengthener.scripts.modes.analyze",
        "workspace.skills.audit_formulation_strengthener.scripts.modes.search",
        "workspace.skills.audit_formulation_strengthener.scripts.modes.synthesize",
    )
    for t in targets:
        mod = __import__(t, fromlist=["chat_json"])
        monkeypatch.setattr(mod, "chat_json", mock_llm.chat_json)
    return mock_llm


@pytest.fixture
def sample_vnd_files(tmp_path: Path) -> dict[str, Path]:
    """Создаёт 2 tmp-файла ВНД с реальным текстом + 1 пустой файл.

    Возвращает dict ``{"file1": Path, "file2": Path, "empty": Path}``.
    ``empty`` — пустой файл для теста ``vnd_empty``.
    """
    file1 = tmp_path / "vnd1.txt"
    file1.write_text(
        "1.1 Срок хранения персональных данных — 1 год с момента сбора.\n\n"
        "1.2 Уничтожение данных осуществляется в срок не более 30 дней.\n",
        encoding="utf-8",
    )
    file2 = tmp_path / "vnd2.txt"
    file2.write_text(
        "2.1 Передача ПДн третьим лицам допускается только с письменного согласия.\n",
        encoding="utf-8",
    )
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    return {"file1": file1, "file2": file2, "empty": empty}


def make_analyze_result(
    *,
    normalized: str = "нормализованная формулировка",
    severity: str = "средняя",
    key_concepts: list[str] | None = None,
    suggested_vnd_sections: list[str] | None = None,
    raw_text: str = "raw text",
) -> dict:
    """Собрать dict в формате результата ``modes.analyze.run``."""
    return {
        "status": "success",
        "data": {
            "raw_text": raw_text,
            "normalized": normalized,
            "key_concepts": key_concepts if key_concepts is not None else ["пдн", "хранение"],
            "severity": severity,
            "suggested_vnd_sections": suggested_vnd_sections if suggested_vnd_sections is not None else [],
        },
    }


def make_search_result(
    *,
    findings: list[dict] | None = None,
    chunks_total: int = 3,
    chunks_processed: int = 3,
    chunks_failed: int = 0,
    chunks_relevant_total: int | None = None,
    normalized_violation_used: str = "нормализованная формулировка",
) -> dict:
    """Собрать dict в формате результата ``modes.search.run``."""
    if findings is None:
        findings = [
            {
                "evidence_id": "F1",
                "source_file": "vnd1.txt",
                "chunk_index": 0,
                "text_excerpt": "Срок хранения ПДн — 1 год.",
                "relation_type": "прямое_противоречие",
                "relevance_score": 0.85,
                "why_matches": "прямо противоречит формулировке отклонения",
            },
            {
                "evidence_id": "F2",
                "source_file": "vnd2.txt",
                "chunk_index": 1,
                "text_excerpt": "Передача ПДн третьим лицам.",
                "relation_type": "контекст",
                "relevance_score": 0.4,
                "why_matches": "контекст по смежной теме",
            },
        ]
    return {
        "status": "success",
        "data": {
            "vnd_findings": findings,
            "vnd_chunks_total": chunks_total,
            "chunks_processed": chunks_processed,
            "chunks_failed": chunks_failed,
            "chunks_relevant_total": (
                chunks_relevant_total if chunks_relevant_total is not None else len(findings)
            ),
            "top_k_candidates": 10,
            "min_relevance_score": 0.3,
            "normalized_violation_used": normalized_violation_used,
        },
    }

"""Фикстуры для тестов ``audit_formulation_strengthener``.

Тесты работают против:

* **in-memory моков LLM** — без сетевых вызовов, детерминированно;
* **in-memory моков ВНД** — текст-фикстуры в temp-файлах, не нужен
  реальный PDF/DOCX-парсинг;
* **без реального ``run_canonical_pipeline``** — заменён фейк-чанкером,
  который возвращает заранее подготовленный список ``VndChunk``.

Это позволяет unit-тестировать логику ``modes/`` (analyze, search,
synthesize) и CLI без поднятия PostgreSQL / Ollama.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Repo-root resolution
# ---------------------------------------------------------------------------


def _find_repo_root(start: Path) -> Path:
    """Найти корень репо (где лежит ``workspaces_nanobot/``)."""
    cur = start.resolve()
    for _ in range(8):
        if (cur / "workspaces_nanobot" / "config.py").is_file():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    raise RuntimeError(
        f"Cannot find repo root from {start}: workspaces_nanobot/config.py not found"
    )


REPO_ROOT = _find_repo_root(Path(__file__).parent)
SKILL_DIR = (
    REPO_ROOT / "workspaces_nanobot" / "workspace" / "skills" / "audit_formulation_strengthener"
)
SCRIPTS_DIR = SKILL_DIR / "scripts"
CLI_PATH = SCRIPTS_DIR / "cli.py"
PROMPTS_DIR = SKILL_DIR / "prompts"


# ---------------------------------------------------------------------------
# LLM mocks
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_llm_analyze():
    """Мок ``call_llm_json`` для analyze-фазы."""
    canned = {
        "normalized": "В организации установлен срок хранения персональных данных, составляющий один год.",
        "key_concepts": ["срок хранения", "персональные данные", "1 год"],
        "severity": "высокая",
        "suggested_vnd_sections": ["Сроки хранения ПДн"],
    }

    def _patch(system: str, user: str, operation: str) -> dict[str, Any]:
        if operation == "analyze":
            return dict(canned)
        raise RuntimeError(f"Unexpected operation in mock: {operation}")

    return _patch


@pytest.fixture
def mock_llm_search_map():
    """Мок ``call_llm_json`` для search map-фазы.

    Возвращает разные оценки для разных relation_type — чтобы можно
    было проверить фильтрацию и re-rank.
    """

    def _patch(system: str, user: str, operation: str) -> dict[str, Any]:
        if operation != "search_map":
            raise RuntimeError(f"Unexpected operation in mock: {operation}")
        # Извлечь excerpt из system (после маркера ``## Фрагмент ВНД``).
        excerpt_marker = "## Фрагмент ВНД"
        if excerpt_marker in system:
            excerpt_part = system.split(excerpt_marker, 1)[1][:500]
        else:
            excerpt_part = ""

        # Грубая логика: по содержимому excerpt выбираем type + score.
        if "пять лет" in excerpt_part and "1" in excerpt_part:
            return {
                "relation_type": "прямое_противоречие",
                "relevance_score": 0.92,
                "why_matches": "Установлено прямое противоречие.",
            }
        if "персональные данные" in excerpt_part:
            return {
                "relation_type": "косвенное_отношение",
                "relevance_score": 0.55,
                "why_matches": "Косвенное отношение.",
            }
        if "нерелевантный" in excerpt_part.lower():
            return {
                "relation_type": "нерелевантно",
                "relevance_score": 0.05,
                "why_matches": "",
            }
        return {
            "relation_type": "контекст",
            "relevance_score": 0.40,
            "why_matches": "Общий контекст.",
        }

    return _patch


@pytest.fixture
def mock_llm_synthesize():
    """Мок ``call_llm_json`` для synthesize-фазы."""
    canned = {
        "title": "Анализ отклонения: срок хранения ПДн",
        "violation_summary": [
            "В организации установлен срок хранения персональных данных, составляющий один год."
        ],
        "established_facts": [
            "В соответствии с пунктом ВНД минимальный срок хранения ПДн — пять лет."
        ],
        "deviation_analysis": [
            "Установлено прямое противоречие между фактическим сроком хранения и требованиями ВНД."
        ],
        "vnd_citations": [
            {
                "source_file": "vnd1.txt",
                "section_title": "5.4 Сроки хранения",
                "section_path": "5 / 5.4",
                "excerpt": "Срок хранения ПДн — не менее пяти лет.",
                "relation_type": "прямое_противоречие",
                "relation_explanation": "Прямое противоречие.",
            }
        ],
        "verdict": {
            "category": "высокая",
            "verdict_text": ["Отклонение классифицируется как высокой тяжести."],
        },
        "recommended_formulation": [
            "В ходе проверки установлено нарушение пункта 5.4 ВНД в части хранения ПДн в течение одного года при минимальном установленном сроке пять лет."
        ],
    }

    def _patch(system: str, user: str, operation: str) -> dict[str, Any]:
        if operation == "synthesize":
            return dict(canned)
        raise RuntimeError(f"Unexpected operation in mock: {operation}")

    return _patch


@pytest.fixture
def mock_llm_all(mock_llm_analyze, mock_llm_search_map, mock_llm_synthesize):
    """Мок всех трёх LLM-фаз через единую точку входа."""

    def _patch(system: str, user: str, operation: str) -> dict[str, Any]:
        if operation == "analyze":
            return mock_llm_analyze(system, user, operation)
        if operation == "search_map":
            return mock_llm_search_map(system, user, operation)
        if operation == "synthesize":
            return mock_llm_synthesize(system, user, operation)
        raise RuntimeError(f"Unexpected operation in mock: {operation}")

    return _patch


# ---------------------------------------------------------------------------
# VND mocks
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_vnd_chunks():
    """Список заранее подготовленных ``VndChunk``-подобных dicts.

    Используется для подмены ``vnd_io.prepare_vnd``.
    """
    from datetime import datetime

    now = datetime.now().isoformat()
    return [
        {
            "source_file": "vnd1.txt",
            "index": 0,
            "text": "Срок хранения персональные данные — не менее пяти лет. (пункт 5.4)",
            "section_title": "5.4 Сроки хранения",
            "section_path": "5 / 5.4 Сроки хранения",
            "token_estimate": 50,
        },
        {
            "source_file": "vnd1.txt",
            "index": 1,
            "text": "Раздел о правах субъектов персональные данные.",
            "section_title": "6.1 Права субъектов",
            "section_path": "6 / 6.1 Права",
            "token_estimate": 30,
        },
        {
            "source_file": "vnd2.txt",
            "index": 0,
            "text": "Нерелевантный фрагмент про офисные процедуры.",
            "section_title": "1.1 Общие положения",
            "section_path": "1 / 1.1",
            "token_estimate": 20,
        },
    ]


@pytest.fixture
def mock_prepare_vnd(sample_vnd_chunks):
    """Мок ``vnd_io.prepare_vnd`` — возвращает заранее заготовленные чанки.

    Подменяет на уровне модуля ``modes.search`` (и ``modes.synthesize``,
    если тот начнёт ходить в ВНД).
    """

    def _fake(*args: Any, **kwargs: Any):
        return {
            "vnd_paths": kwargs.get("vnd_paths") or [],
            "chunks": sample_vnd_chunks,
            "cache_key": "test_cache_key_" + "x" * 56,
            "size_estimate": {
                "vnd_files": len(kwargs.get("vnd_paths") or []),
                "total_chars": 1000,
                "total_pages_estimated": 1,
                "chunks_estimated": len(sample_vnd_chunks),
                "map_batches_planned": len(sample_vnd_chunks),
            },
        }

    return _fake


@pytest.fixture
def tmp_vnd_files(tmp_path: Path) -> list[str]:
    """Создать temp-файлы с короткими ВНД-фрагментами."""
    files: list[str] = []
    for i, content in enumerate(
        [
            "Раздел 5.4 Сроки хранения: персональные данные хранятся пять лет.",
            "Раздел 6.1 Права субъектов персональные данные.",
        ]
    ):
        p = tmp_path / f"vnd{i}.txt"
        p.write_text(content, encoding="utf-8")
        files.append(str(p))
    return files


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def scripts_dir() -> Path:
    """Абсолютный путь к ``scripts/`` skill'а."""
    return SCRIPTS_DIR


@pytest.fixture
def cli_path() -> Path:
    """Абсолютный путь к ``scripts/cli.py``."""
    return CLI_PATH

"""Загрузка системных промптов и формирование length/question инструкций.

Модуль НЕ называется ``prompts.py``, чтобы не конфликтовать с
существующим ``scripts/prompts.py`` (там лежит ``build_batch_user_message``
и парсер LLM-JSON).
"""
from __future__ import annotations

from pathlib import Path


_SKILL_ROOT = Path(__file__).resolve().parents[1]
_PROMPTS_DIR = _SKILL_ROOT / "prompts"

_PROMPT_FILES = {
    "summarize_system": _PROMPTS_DIR / "summarize_system.md",
    "reduce_system": _PROMPTS_DIR / "reduce_system.md",
    "section_reduce_system": _PROMPTS_DIR / "section_reduce_system.md",
}


def load_prompt(name: str) -> str:
    """Прочитать системный промпт из ``prompts/<name>.md``."""
    p = _PROMPT_FILES.get(name)
    if p is None or not p.is_file():
        raise FileNotFoundError(
            f"Не найден файл промпта: {p}. Промпты лежат в "
            "workspace/skills/legal_summarizer/prompts/."
        )
    return p.read_text(encoding="utf-8")


LENGTH_INSTRUCTIONS = {
    "brief": "1 абзац, 150-250 слов: что это за документ и ключевые условия в двух-трёх фразах. НЕ превышай 250 слов.",
    "detailed": "по разделам документа, 800-1200 слов: каждый раздел простым языком. НЕ превышай 1200 слов.",
}


QUESTION_INSTRUCTION_TEMPLATE = (
    "Пользователь задал конкретный вопрос: «{question}»\n"
    "Ищи в chunk'е / саммари ТОЛЬКО факты по этому вопросу.\n"
    "Если ничего не относится — пропусти (для map) "
    "или напиши «В документе не нашёл ответа» (для reduce).\n"
    "НЕ описывай документ целиком, отвечай только на вопрос.\n"
    "ОБЪЁМ: максимум 200-300 слов, прямой ответ по существу вопроса. "
    "Без вводных фраз, без перечисления статей закона (только релевантные), "
    "без подробного изложения каждого аспекта."
)


def system_instruction(length: str, question: str | None) -> str:
    """Подготовить инструкцию для {length_instruction} в системном промпте.

    Если передан ``question`` — инструкция фокусирует LLM на ответе на
    конкретный вопрос (длину игнорируем — ответ всегда краткий).
    Иначе — стандартная инструкция по объёму.
    """
    if question:
        return QUESTION_INSTRUCTION_TEMPLATE.format(question=question)
    return LENGTH_INSTRUCTIONS.get(length, LENGTH_INSTRUCTIONS["brief"])


__all__ = [
    "load_prompt",
    "LENGTH_INSTRUCTIONS",
    "QUESTION_INSTRUCTION_TEMPLATE",
    "system_instruction",
]

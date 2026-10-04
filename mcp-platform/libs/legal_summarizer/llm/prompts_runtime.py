"""Загрузка системных промптов и формирование length/question инструкций.

Модуль НЕ называется ``prompts.py``, чтобы не конфликтовать с
существующим ``scripts/prompts.py`` (там лежит ``build_batch_user_message``
и парсер LLM-JSON).
"""
from __future__ import annotations

from pathlib import Path

#: Промпты лежат с доменом, в ``libs/legal_summarizer/skill/prompts/`` -
#: рядом с ``SKILL.md`` и ``references/``. Пока payload не переехал в
#: capability (п. 11.4), держать его в ``capabilities/`` нельзя: страж
#: ``test_no_capability_without_operations`` требует у каждого каталога в
#: ``capabilities/`` наличие ``tools/*.py``, а
#: ``test_every_capability_on_disk_is_in_the_registry`` - запись в реестре
#: ``CAPABILITIES``. Половинчатая capability - это заготовка, которую реестр
#: стражей называет недоношенной.
#:
#: Якорь — от этого файла, а не от ``cwd``: ``parents[1]`` от
#: ``libs/legal_summarizer/llm/prompts_runtime.py`` — корень домена.
#: Прежний ``parents[2]`` указывал на каталог скилла агента и после
#: переноса вёл в несуществующий ``libs/prompts/`` — конвейер падал на
#: ``load_prompt`` ещё до первого LLM-вызова (см. ``llm.calls``).
_DOMAIN_ROOT = Path(__file__).resolve().parents[1]
_PROMPTS_DIR = _DOMAIN_ROOT / "skill" / "prompts"

_PROMPT_FILES = {
    "summarize_system": _PROMPTS_DIR / "summarize_system.md",
    "reduce_system": _PROMPTS_DIR / "reduce_system.md",
    "section_reduce_system": _PROMPTS_DIR / "section_reduce_system.md",
}


def load_prompt(name: str) -> str:
    """Прочитать системный промпт из ``skill/prompts/<name>.md``."""
    p = _PROMPT_FILES.get(name)
    if p is None or not p.is_file():
        raise FileNotFoundError(
            f"Не найден файл промпта: {p}. Промпты лежат с доменом, в "
            "mcp-platform/libs/legal_summarizer/skill/prompts/ — рядом с "
            "SKILL.md, а не в capabilities/: тот каталог зарезервирован под "
            "операции, и страж test_no_capability_without_operations требует "
            "в нём tools/*.py (см. комментарий выше)."
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


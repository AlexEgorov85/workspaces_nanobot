"""
Follow Up 2.0 — Query Understanding.

Извлекает из запроса пользователя:
- intent (тип запроса)
- номера КМ
- тему проверки
- ключевые слова для поиска

Комбинирует regex (для быстрого извлечения КМ) + LLM (для intent и темы).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from backend.config import get_settings
from backend.core import identity
from backend.llm.client import generate_sync
from backend.llm.prompts.router import ROUTER_SYSTEM, ROUTER_USER_TEMPLATE
from backend.rag.conversation import last_mentioned_km, needs_reference_resolution

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────

class Intent:
    FOLLOWUP = "FOLLOWUP"       # Поиск по прошлым проверкам
    HYPOTHESIS = "HYPOTHESIS"   # Гипотезы для новой проверки
    RECHECK = "RECHECK"         # Анализ для репроверки
    ANALYTICS = "ANALYTICS"     # Статистика и аналитика
    REPORT = "REPORT"           # Формирование отчёта
    KM_DETAIL = "KM_DETAIL"     # Подробности по конкретной КМ (drill-down)
    EXECUTION_CONTROL = "EXECUTION_CONTROL"  # Контроль исполнения поручений
    GENERAL = "GENERAL"         # Общий вопрос


@dataclass
class QueryContext:
    raw_query: str
    intent: str = Intent.GENERAL
    km_numbers: List[str] = field(default_factory=list)
    topic: Optional[str] = None
    keywords: List[str] = field(default_factory=list)
    search_query: str = ""     # оптимизированный запрос для поиска
    confidence: float = 0.5
    resolved_from_history: bool = False  # КМ/тема подтянуты из истории диалога
    has_attachment: bool = False         # к сообщению приложен файл
    attachment_text: Optional[str] = None  # текст вложения (ответ профильника)


# ──────────────────────────────────────────────────────────────────
# Regex patterns
# ──────────────────────────────────────────────────────────────────

# Разбор номера КМ живёт в backend/core/identity.py — единственном владельце
# формы. Локальной копии регекса здесь больше нет: копии расходились, и
# «КМ 99-12345» через пробел не находился ни одной из них.

# Слова-маркеры intent (быстрая предклассификация)
_HYPOTHESIS_KEYWORDS = re.compile(
    r"начинаю|начать|новую|новая проверка|гипотез|что проверить|с чего начать",
    re.IGNORECASE,
)
_RECHECK_KEYWORDS = re.compile(
    r"репровер|повторн|перепровер|что можно провер|устранен|устранили",
    re.IGNORECASE,
)
_ANALYTICS_KEYWORDS = re.compile(
    # Строго статистика/агрегаты — не конкретные факты
    r"статистик|сколько\s+(?:всего|было|найдено|нарушений)|"
    r"частот[а-я]*\s+(?:нарушений|отклонений)|тренд|динамик|"
    r"топ[\s-]+\d|наиболее\s+частых|по\s+категориям|по\s+кварталам|"
    r"процент|доля\s+нарушений|аналитическ",
    re.IGNORECASE,
)
_FOLLOWUP_KEYWORDS = re.compile(
    # Поиск конкретных фактов из прошлых проверок
    r"прошл|были|предыдущ|раньше|история|ранее|похожи|аналогичн|"
    r"какие КМ|какие распоряжени|"
    # «какие X использовались/применялись/нашли/выявили/указаны» — конкретный вопрос
    r"какие\s+\w+\s+(?:использовал|применял|нашл|выявил|указан|упоминал|встречал)|"
    r"какие\s+(?:код[ыа]|нарушени|отклонени|меры|рекомендаци|ошибк|проблем|риск)|"
    r"что\s+(?:нашл|выявил|обнаружил|находил|нарушал|фиксировал)|"
    r"какой\s+(?:код|нарушени|результат|вывод|итог)",
    re.IGNORECASE,
)
# Контроль исполнения поручений: пришёл ответ профильника / проверить исполнение.
# Проверяется ПЕРВЫМ — это самый специфичный интент.
_EXEC_CONTROL_KEYWORDS = re.compile(
    r"поручени|ответ\s+профильн|профильник|контроль\s+исполнени|"
    r"исполнение\s+поручени|снять\s+с\s+(?:централизованного\s+)?контрол|"
    r"отписк|пришел\s+ответ|пришёл\s+ответ|прислали\s+ответ|"
    r"закрыт[ьо]\s+поручени|проверь\s+исполнени",
    re.IGNORECASE,
)
# Структурные маркеры письма-ответа из СЭД (для длинных вставок)
_EXEC_DOC_MARKERS = re.compile(
    r"об\s+исполнении\s+поручени|поручение\s*№|выполнено\.|"
    r"в\s+ответ\s+на|акт\s+УВА|просим\s+.{0,30}снять",
    re.IGNORECASE,
)

# Drill-down: «расскажи подробнее», «какие базы», «какие системы», «покажи код»
_KM_DETAIL_KEYWORDS = re.compile(
    r"подробнее|поподробн|расскажи про|расскажи о|расскажи об|"
    r"какие\s+(?:баз|систем|процесс|сервис|приложен|БД|таблиц|регламент|"
    r"подразделен|документ|нормативн|инструкц)|"
    r"какая\s+(?:баз|систем|таблиц|инструкц)|"
    r"что\s+(?:использовалось|использовали|применяли|применялось|анализировал)|"
    r"покажи\s+(?:код|sql|sql-код|пример|кусок|фрагмент|выдержк|цитат)|"
    r"приведи\s+(?:цитат|пример|выдержк|кусок|фрагмент)|"
    r"какой\s+(?:период|объ[её]м|охват|scope)",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────────
# Extraction
# ──────────────────────────────────────────────────────────────────

def _extract_km_numbers(text: str) -> List[str]:
    """Номера КМ из текста, канонизированные (backend/core/identity.py)."""
    return identity.parse(text)


def _quick_classify(query: str, has_km: bool = False,
                    has_attachment: bool = False) -> Optional[str]:
    """Быстрая классификация по ключевым словам (без LLM).

    Приоритет: EXECUTION_CONTROL > KM_DETAIL > HYPOTHESIS > RECHECK >
    FOLLOWUP > ANALYTICS. EXECUTION_CONTROL первым — самый специфичный.
    FOLLOWUP намеренно стоит ПЕРЕД ANALYTICS — конкретные вопросы «какие X»
    не должны попадать в статистику.
    """
    # Структурные сигналы контроля исполнения:
    # 1) вложение (ответ профильника загружают файлом)
    # 2) длинная вставка с маркерами письма-ответа
    # 3) явные ключевые слова
    if has_attachment:
        return Intent.EXECUTION_CONTROL
    if len(query) > 1500 and _EXEC_DOC_MARKERS.search(query):
        return Intent.EXECUTION_CONTROL
    if _EXEC_CONTROL_KEYWORDS.search(query):
        return Intent.EXECUTION_CONTROL
    if _KM_DETAIL_KEYWORDS.search(query):
        return Intent.KM_DETAIL
    if _HYPOTHESIS_KEYWORDS.search(query):
        return Intent.HYPOTHESIS
    if _RECHECK_KEYWORDS.search(query):
        return Intent.RECHECK
    # FOLLOWUP перед ANALYTICS — «какие коды» это факт, а не статистика
    if _FOLLOWUP_KEYWORDS.search(query):
        return Intent.FOLLOWUP
    if _ANALYTICS_KEYWORDS.search(query):
        return Intent.ANALYTICS
    return None


# Слова, которые надо вырезать чтобы получить «тему» из запроса
_TOPIC_NOISE = re.compile(
    r"\b(начинаю|начать|новую|новая|проверку|проверки|проверка|"
    r"гипотез[а-я]*|что\s+проверить|с\s+чего\s+начать|"
    r"репровер[а-я]+|повторн[а-я]+|перепровер[а-я]+|устранен[а-я]+|устранили|"
    r"статистик[а-я]+|аналитик[а-я]+|сколько|частот[а-я]+|тренд[а-я]*|динамик[а-я]+|"
    r"прошл[а-я]+|были|предыдущ[а-я]+|раньше|история|ранее|похожи[а-я]*|аналогичн[а-я]+|"
    r"какие|дай|покажи|расскажи|пожалуйста)\b",
    re.IGNORECASE,
)


def _extract_topic_heuristic(query: str) -> Optional[str]:
    """Извлекает тему запроса простыми эвристиками, без LLM.

    1. Если есть «по <что-то>» — берём то, что после «по».
    2. Иначе — вычищаем шумовые слова и оставшееся считаем темой.
    """
    m = re.search(r"\bпо\s+(.+?)(?:[,.;!?]|$)", query, re.IGNORECASE)
    candidate = m.group(1).strip() if m else query
    cleaned = _TOPIC_NOISE.sub(" ", candidate)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.;:-—")
    return cleaned or None


def understand_query(
    query: str,
    use_llm: bool = True,
    history: Optional[List[dict]] = None,
    has_attachment: bool = False,
    attachment_text: Optional[str] = None,
) -> QueryContext:
    """
    Анализирует запрос пользователя.

    Стратегия:
    1. Regex — извлечение номеров КМ
    2. История — если в текущем запросе нет КМ, но есть «эта проверка / расскажи подробнее»,
       подтягиваем последний упомянутый КМ из истории
    3. Quick classify — ключевые слова + структурные сигналы (вложение)
    4. LLM (если нужно) — точная классификация и извлечение темы
    """
    cfg = get_settings()
    ctx = QueryContext(raw_query=query,
                       has_attachment=has_attachment,
                       attachment_text=attachment_text)

    # 1. Извлекаем номера КМ через regex (запрос + текст вложения)
    ctx.km_numbers = _extract_km_numbers(
        query + " " + (attachment_text or "")[:4000])

    # 2. Резолвинг отсылок к истории («эта КМ», «расскажи подробнее»)
    if not ctx.km_numbers and history and needs_reference_resolution(query, has_explicit_km=False):
        prev_km = last_mentioned_km(history)
        if prev_km:
            ctx.km_numbers = [prev_km]
            ctx.resolved_from_history = True
            logger.info(f"[QU] Резолв из истории: КМ={prev_km}")

    # 3. Быстрая классификация
    quick_intent = _quick_classify(query, has_km=bool(ctx.km_numbers),
                                   has_attachment=has_attachment)

    # 2a. Если regex уверенно сработал и в конфиге отключён LLM-fallback —
    #     не звать LLM. Это экономит ~5-7 секунд на каждом запросе.
    if quick_intent and (not use_llm or cfg.qu_skip_llm_when_quick_classified):
        ctx.intent = quick_intent
        ctx.topic = _extract_topic_heuristic(query)
        ctx.confidence = 0.85
        search_parts = [query]
        if ctx.topic:
            search_parts.append(ctx.topic)
        ctx.search_query = " ".join(search_parts)
        logger.info(
            f"[QU/quick] intent={ctx.intent}, КМ={ctx.km_numbers}, тема={ctx.topic}"
        )
        return ctx

    # 3. LLM классификация (когда regex не уверен)
    try:
        messages = [
            {"role": "system", "content": ROUTER_SYSTEM},
            {"role": "user", "content": ROUTER_USER_TEMPLATE.format(query=query)},
        ]
        raw = generate_sync(messages, max_tokens=300, temperature=0.0)

        # Парсим JSON
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            ctx.intent = data.get("intent", quick_intent or Intent.GENERAL)
            # Объединяем КМ из regex и LLM
            llm_km = data.get("km_numbers", [])
            all_km = list(dict.fromkeys(ctx.km_numbers + llm_km))
            ctx.km_numbers = all_km
            ctx.topic = data.get("topic")
            ctx.keywords = data.get("keywords", [])
            ctx.confidence = data.get("confidence", 0.7)
        else:
            ctx.intent = quick_intent or Intent.GENERAL

    except Exception as e:
        logger.warning(f"[QU] Ошибка LLM классификации: {e}")
        ctx.intent = quick_intent or Intent.GENERAL

    # Формируем оптимизированный запрос для поиска
    search_parts = [query]
    if ctx.topic:
        search_parts.append(ctx.topic)
    if ctx.keywords:
        search_parts.extend(ctx.keywords[:5])
    ctx.search_query = " ".join(search_parts)

    logger.info(
        f"[QU] intent={ctx.intent}, КМ={ctx.km_numbers}, "
        f"тема={ctx.topic}, conf={ctx.confidence:.2f}"
    )
    return ctx

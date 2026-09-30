"""
Follow Up 2.0 — LLM-based Deviation Extractor.

При индексации каждый чанк прогоняется через LLM для извлечения
отклонений и нарушений. Результаты сохраняются в SQLite.

Для больших документов (100+ страниц) используется батчевая обработка
с ограничением количества чанков на документ (только наиболее информативные).
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable, Dict, List, Optional

from backend.config import get_settings
from backend.llm.client import generate_sync
from backend.llm.prompts.extractor import (
    EXTRACTOR_SYSTEM,
    EXTRACTOR_USER_TEMPLATE,
    EXTRACTOR_USER_BATCH_TEMPLATE,
)

logger = logging.getLogger(__name__)

# Базовая задержка между LLM вызовами для внешних API (сек) — чтобы не уйти в 429.
# Для GigaChat во внутренней сети банка используется cfg.gigachat_delay (8-9с).
_RATE_LIMIT_DELAY_DEFAULT = 2.5
_last_llm_call: float = 0.0


def _current_rate_limit_delay() -> float:
    cfg = get_settings()
    if cfg.llm_mode == "gigachat":
        return cfg.gigachat_delay
    return _RATE_LIMIT_DELAY_DEFAULT


def _rate_limited_generate(messages, max_tokens=800, temperature=0.0) -> str:
    """Вызов LLM с rate limiter и retry при 429.

    Если в конфиге задана отдельная модель для extractor (llm_extractor_model) —
    используем её. Иначе берётся дефолтная модель.
    """
    global _last_llm_call
    delay = _current_rate_limit_delay()
    elapsed = time.perf_counter() - _last_llm_call
    if elapsed < delay:
        time.sleep(delay - elapsed)
    _last_llm_call = time.perf_counter()

    extractor_model = get_settings().llm_extractor_model

    for attempt in range(4):
        try:
            return generate_sync(
                messages,
                model=extractor_model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "rate" in err_str.lower():
                wait = 5 * (attempt + 1)
                logger.warning(f"[Extractor] Rate limit, жду {wait}s (попытка {attempt+1})")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Rate limit: превышено число попыток")

# Ключевые слова, указывающие на наличие нарушений в чанке
_VIOLATION_HINTS = re.compile(
    r"нарушен|отклонен|несоответств|замечан|недостат|не устранен|не исполнен|"
    r"не соответств|превышен|нарушение|ошибк|отсутств|не выполнен",
    re.IGNORECASE | re.UNICODE,
)


def _chunk_likely_has_violations(text: str) -> bool:
    """Быстрая предфильтрация: содержит ли чанк ключевые слова нарушений."""
    return bool(_VIOLATION_HINTS.search(text))


def _repair_truncated_array(s: str) -> Optional[list]:
    """
    Спасает обрезанный на середине JSON-массив: отрезает по последнему
    полностью закрытому объекту и закрывает массив. Возвращает list или None.

    Причина: LLM с reasoning или жёстким max_tokens может оборвать ответ —
    раньше такой батч терялся ЦЕЛИКОМ (одна из причин пустых отклонений).
    """
    last = s.rfind("}")
    while last != -1:
        candidate = s[: last + 1].rstrip().rstrip(",") + "]"
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
        last = s.rfind("}", 0, last)
    return None


def _extract_json_array(raw_json: str) -> Optional[list]:
    """
    Достаёт JSON-массив из ответа LLM.
    Возвращает: list (в т.ч. пустой = «нарушений нет») или None = ПАРСИНГ
    ПРОВАЛЕН (вызывающий код должен деградировать на почанковую обработку,
    а не молча терять батч).
    """
    if not raw_json:
        return None
    cleaned = re.sub(r"```(?:json)?\s*", "", raw_json, flags=re.IGNORECASE)
    cleaned = cleaned.replace("```", "")

    start = cleaned.find("[")
    if start != -1:
        tail = cleaned[start:]
        end = tail.rfind("]")
        if end != -1:
            # Полный массив (возможно, с мусором внутри)
            try:
                parsed = json.loads(tail[: end + 1])
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
        # Закрывающей скобки нет (обрезан) или массив битый —
        # спасаем целые объекты
        repaired = _repair_truncated_array(tail)
        if repaired is not None:
            logger.warning(
                f"[Extractor] JSON обрезан/битый — спасено объектов: {len(repaired)}")
            return repaired

    # Массива нет: одиночный объект без скобок массива
    obj = re.search(r"\{[\s\S]*?\}", cleaned)
    if obj:
        try:
            return [json.loads(obj.group(0))]
        except json.JSONDecodeError:
            pass
    # «Нарушений нет» текстом — осознанно пустой результат
    if re.search(r"нарушени[йя]\s+нет|пустой\s+массив", cleaned, re.IGNORECASE):
        return []
    return None


def _parse_deviations(
    raw_json: str,
    check_id: str,
    chunk_index: int,
    document_id: int,
    use_source_chunk_index: bool = False,
) -> List[Dict]:
    """Парсит JSON-ответ LLM в список словарей для БД."""
    parsed = _extract_json_array(raw_json)
    if parsed is None:
        logger.warning(
            f"[Extractor] КМ {check_id} чанк {chunk_index}: JSON не распарсен. "
            f"Ответ LLM (first 200): {(raw_json or '')[:200]!r}"
        )
        return []

    results = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        desc = (item.get("description") or "").strip()
        if not desc or len(desc) < 10:
            continue
        category = _normalize_category((item.get("category") or "Прочие").strip())
        # При батч-извлечении LLM может вернуть source_chunk_index в теле ответа
        src_idx = chunk_index
        if use_source_chunk_index and item.get("source_chunk_index") is not None:
            try:
                src_idx = int(item["source_chunk_index"])
            except (ValueError, TypeError):
                pass
        results.append({
            "document_id": document_id,
            "check_id": check_id,
            "category": category,
            "description": desc,
            "severity": _normalize_severity(item.get("severity")),
            "source_chunk_index": src_idx,
            "financial_impact_rub": _coerce_money(item.get("financial_impact_rub"), desc),
            "affected_systems": _coerce_json_list(item.get("affected_systems")),
            "regulation_refs": _coerce_json_list(item.get("regulation_refs")),
            "affected_count": _coerce_int(item.get("affected_count")),
            "responsible_unit": _coerce_str(item.get("responsible_unit")),
            "recommendation": _coerce_str(item.get("recommendation")),
        })
    return results


# ──────────────────────────────────────────────────────────────────
# Coercion helpers (мягко приводят разный JSON-мусор от LLM к нужному типу)
# ──────────────────────────────────────────────────────────────────

_CATEGORY_MAP = {
    # Регуляторные
    "регуляторное": "Регуляторные", "регуляторная": "Регуляторные",
    "регуляторный": "Регуляторные", "регуляторные": "Регуляторные",
    # Информационная безопасность
    "иб": "Информационная безопасность",
    "информационная безопасность": "Информационная безопасность",
    "кибербезопасность": "Информационная безопасность",
    "безопасность": "Информационная безопасность",
    # Управление доступом
    "управление доступом": "Управление доступом",
    "доступ": "Управление доступом",
    "iam": "Управление доступом",
    # Управление изменениями
    "управление изменениями": "Управление изменениями",
    "change management": "Управление изменениями",
    "изменения": "Управление изменениями",
    # Управление инцидентами
    "управление инцидентами": "Управление инцидентами",
    "инциденты": "Управление инцидентами",
    "incident management": "Управление инцидентами",
    # Контрольные
    "контрольное": "Контрольные", "контрольная": "Контрольные",
    "контрольные": "Контрольные", "свк": "Контрольные",
    # Процессные
    "процессное": "Процессные", "процессная": "Процессные",
    "процессные": "Процессные",
    # Технические
    "техническое": "Технические", "техническая": "Технические",
    "технические": "Технические",
    # Документационные
    "документационное": "Документационные",
    "документационная": "Документационные",
    "документационные": "Документационные",
    "документация": "Документационные",
    # Аутсорсинг
    "аутсорсинг": "Аутсорсинг",
    "третьи стороны": "Аутсорсинг",
    "вендоры": "Аутсорсинг",
    # ПДн
    "защита персональных данных": "Защита персональных данных",
    "пдн": "Защита персональных данных",
    "персональные данные": "Защита персональных данных",
    "152-фз": "Защита персональных данных",
    # BCP/DR
    "непрерывность деятельности": "Непрерывность деятельности",
    "bcp": "Непрерывность деятельности",
    "dr": "Непрерывность деятельности",
    "восстановление": "Непрерывность деятельности",
    # Прочие
    "прочее": "Прочие", "прочие": "Прочие", "другое": "Прочие",
}

_VALID_SEVERITIES = {"критичное", "существенное", "формальное"}


def _normalize_category(category: str) -> str:
    return _CATEGORY_MAP.get(category.strip().lower(), category)


def _normalize_severity(value) -> str:
    s = (value or "формальное").strip().lower()
    if s in _VALID_SEVERITIES:
        return s
    # Синонимы
    if s in {"высокий", "high", "critical", "критическое"}:
        return "критичное"
    if s in {"средний", "medium", "значительное"}:
        return "существенное"
    if s in {"низкий", "low", "minor", "процедурное"}:
        return "формальное"
    return "формальное"


# Парсер русских денежных выражений: «12,5 млн руб.», «420 тыс. ₽», «1 200 000 рублей»
_MONEY_RE = re.compile(
    r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*(млрд|млн|тыс\.?|k|m|b)?\s*(?:руб|₽|rub)",
    re.IGNORECASE,
)
_MULTIPLIER = {
    "млрд": 1_000_000_000, "b": 1_000_000_000,
    "млн": 1_000_000, "m": 1_000_000,
    "тыс": 1_000, "тыс.": 1_000, "k": 1_000,
}


def _coerce_money(value, fallback_text: str = "") -> Optional[float]:
    """LLM может вернуть число, строку '12 500 000', '12,5 млн руб.' или null.
    Если LLM ничего не дал — пытаемся вытащить сумму из исходного описания.
    """
    if value is None or value == "":
        return _extract_money_from_text(fallback_text)
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        # Может быть «12500000» или «12,5 млн руб.»
        clean = value.replace("\xa0", " ").strip()
        if not clean:
            return _extract_money_from_text(fallback_text)
        # Сначала пробуем как число
        digits = re.sub(r"[^\d.,-]", "", clean).replace(",", ".")
        try:
            num = float(digits)
            if num > 0:
                # Ищем суффикс в исходной строке
                m = re.search(r"(млрд|млн|тыс\.?|k|m|b)", clean, re.IGNORECASE)
                if m:
                    suffix = m.group(1).lower().rstrip(".")
                    num *= _MULTIPLIER.get(suffix, 1)
                return num
        except ValueError:
            pass
        return _extract_money_from_text(clean) or _extract_money_from_text(fallback_text)
    return None


def _extract_money_from_text(text: str) -> Optional[float]:
    if not text:
        return None
    m = _MONEY_RE.search(text)
    if not m:
        return None
    raw_num = m.group(1).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        num = float(raw_num)
    except ValueError:
        return None
    suffix = (m.group(2) or "").lower().rstrip(".")
    num *= _MULTIPLIER.get(suffix, 1)
    return num if num > 0 else None


def _coerce_json_list(value) -> Optional[str]:
    """Превращает list[str] в JSON-строку для хранения в SQLite. None если пусто."""
    if value is None:
        return None
    if isinstance(value, str):
        # Если LLM вернул строку через запятую — разобьём
        items = [s.strip() for s in re.split(r"[,;]", value) if s.strip()]
    elif isinstance(value, list):
        items = [str(s).strip() for s in value if str(s).strip()]
    else:
        return None
    if not items:
        return None
    return json.dumps(items, ensure_ascii=False)


def _coerce_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        if digits:
            try:
                n = int(digits)
                return n if n > 0 else None
            except ValueError:
                return None
    return None


def _coerce_str(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in {"null", "none", "n/a", "—", "-"}:
        return None
    return s


def _field_richness(d: Dict) -> int:
    """Сколько полезных полей заполнено (для выбора лучшего дубля)."""
    return sum(1 for k in ("financial_impact_rub", "affected_systems",
                           "regulation_refs", "affected_count",
                           "responsible_unit", "recommendation")
               if d.get(k) not in (None, "", "[]"))


_NORM_RE = re.compile(r"[^\wа-яё]+", re.IGNORECASE)


def _norm_desc(desc: str) -> str:
    return _NORM_RE.sub(" ", desc.lower()).strip()[:240]


def dedupe_deviations(devs: List[Dict]) -> List[Dict]:
    """
    Дедупликация нарушений одного документа.

    Чанки перекрываются (overlap), и одно нарушение извлекается из соседних
    фрагментов дважды. Сравниваем нормализованные описания; при почти полном
    совпадении оставляем запись с бОльшим числом заполненных полей.
    """
    from difflib import SequenceMatcher
    kept: List[Dict] = []
    for d in devs:
        nd = _norm_desc(d.get("description", ""))
        duplicate_of = None
        for i, k in enumerate(kept):
            nk = _norm_desc(k.get("description", ""))
            if nd == nk or SequenceMatcher(None, nd, nk).ratio() > 0.88:
                duplicate_of = i
                break
        if duplicate_of is None:
            kept.append(d)
        elif _field_richness(d) > _field_richness(kept[duplicate_of]):
            kept[duplicate_of] = d
    if len(kept) < len(devs):
        logger.info(f"[Extractor] Дедуп: {len(devs)} → {len(kept)} "
                    f"(убрано {len(devs) - len(kept)} дублей из перекрытий чанков)")
    return kept


def _build_batch_user_content(check_id: str, batch: List[Dict]) -> str:
    """Формирует единый промпт для батча чанков."""
    parts = []
    for chunk in batch:
        idx = chunk.get("chunk_index", 0)
        text = chunk["text"][:1500]   # ≈ 500 токенов на чанк
        parts.append(f"=== ФРАГМЕНТ chunk_index={idx} ===\n{text}")
    return EXTRACTOR_USER_BATCH_TEMPLATE.format(
        check_id=check_id,
        chunks_text="\n\n".join(parts),
    )


def _parse_batch_deviations(
    raw_json: str,
    check_id: str,
    batch: List[Dict],
    document_id: int,
) -> List[Dict]:
    """Парсит ответ LLM на батч-запрос.

    source_chunk_index берётся из поля ответа (если LLM указал),
    иначе — из первого чанка батча как fallback.
    """
    fallback_idx = batch[0].get("chunk_index", 0) if batch else 0
    return _parse_deviations(raw_json, check_id, fallback_idx, document_id,
                              use_source_chunk_index=True)


def extract_deviations_for_document(
    chunks: List[Dict],
    document_id: int,
    check_id: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> List[Dict]:
    """
    Извлекает отклонения из чанков одного документа.

    Алгоритм:
    1. Предфильтрация — отбираем чанки с ключевыми словами нарушений.
    2. Ограничиваем до extractor_max_chunks наиболее информативных.
    3. Группируем в батчи extractor_batch_size штук → один вызов LLM на батч.
       Батчинг даёт ~batch_size × ускорение при GigaChat с rate-limit 9с/вызов.
    4. Парсим и возвращаем список отклонений.
    """
    cfg = get_settings()
    batch_size = cfg.extractor_batch_size
    max_chunks  = cfg.extractor_max_chunks

    def log(msg: str):
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

    # Предфильтрация
    candidate_chunks = [c for c in chunks if _chunk_likely_has_violations(c["text"])]
    log(f"[Extractor] КМ {check_id}: {len(chunks)} чанков, "
        f"{len(candidate_chunks)} кандидатов с нарушениями")

    if not candidate_chunks:
        return []

    if len(candidate_chunks) > max_chunks:
        log(f"[Extractor] Ограничение до {max_chunks} чанков (было {len(candidate_chunks)})")
        candidate_chunks = candidate_chunks[:max_chunks]

    # Разбиваем на батчи
    batches = [
        candidate_chunks[i: i + batch_size]
        for i in range(0, len(candidate_chunks), batch_size)
    ]
    log(f"[Extractor] Батчей: {len(batches)} (по {batch_size} чанков)")

    all_deviations: List[Dict] = []

    def _extract_single(chunk: Dict) -> List[Dict]:
        """Одиночный чанк — надёжный путь (фолбэк для битых батчей)."""
        chunk_idx = chunk.get("chunk_index", 0)
        messages = [
            {"role": "system", "content": EXTRACTOR_SYSTEM},
            {"role": "user", "content": EXTRACTOR_USER_TEMPLATE.format(
                check_id=check_id, chunk_index=chunk_idx,
                chunk_text=chunk["text"][:2000])},
        ]
        raw = _rate_limited_generate(messages, max_tokens=4000, temperature=0.0)
        return _parse_deviations(raw, check_id, chunk_idx, document_id)

    for b_idx, batch in enumerate(batches):
        chunk_ids = [c.get("chunk_index", i) for i, c in enumerate(batch)]
        try:
            if len(batch) == 1:
                devs = _extract_single(batch[0])
            else:
                messages = [
                    {"role": "system", "content": EXTRACTOR_SYSTEM},
                    {"role": "user",
                     "content": _build_batch_user_content(check_id, batch)},
                ]
                # Плоские 4000 токенов: reasoning-модели тратят бюджет на
                # размышления; прежние 3000 на 5 чанков обрезали JSON,
                # и батч терялся молча
                raw = _rate_limited_generate(messages, max_tokens=4000,
                                             temperature=0.0)
                parsed = _extract_json_array(raw)
                if parsed is None:
                    # Батч не распарсился даже после ремонта —
                    # НЕ теряем: деградируем на почанковую обработку
                    log(f"[Extractor] Батч {b_idx+1}/{len(batches)} не распарсен — "
                        f"переключаюсь на почанковый режим ({len(batch)} чанков)")
                    devs = []
                    for c in batch:
                        try:
                            devs.extend(_extract_single(c))
                        except Exception as ce:
                            logger.error(f"[Extractor] Чанк "
                                         f"{c.get('chunk_index')}: {ce}")
                else:
                    devs = _parse_batch_deviations(raw, check_id, batch,
                                                   document_id)

            if devs:
                all_deviations.extend(devs)
                log(f"[Extractor] Батч {b_idx+1}/{len(batches)} "
                    f"(чанки {chunk_ids}): {len(devs)} нарушений")
        except Exception as e:
            logger.error(f"[Extractor] Ошибка батча {b_idx+1}: {e}")
            continue

    # Дедуп перекрытий чанков (одно нарушение из соседних фрагментов)
    all_deviations = dedupe_deviations(all_deviations)

    log(f"[Extractor] КМ {check_id}: итого {len(all_deviations)} нарушений")
    return all_deviations


def extract_deviations_for_all(
    chunks_by_document: Dict[str, Dict],  # {file_id: {"document_id", "check_id", "chunks"}}
    progress_callback: Optional[Callable[[str], None]] = None,
) -> List[Dict]:
    """Извлекает отклонения для всех документов."""
    all_devs = []
    for file_id, doc_data in chunks_by_document.items():
        devs = extract_deviations_for_document(
            chunks=doc_data["chunks"],
            document_id=doc_data["document_id"],
            check_id=doc_data["check_id"],
            progress_callback=progress_callback,
        )
        all_devs.extend(devs)
    return all_devs

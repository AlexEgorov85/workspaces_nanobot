"""Numbered-list detection — ``structure/list_detection.py``.

Различение нумерованных **разделов** от **списков**.

Сценарии:

* Section sequence:
    1. Общие положения
    <длинный body>
    2. Обязанности сторон
    <длинный body>
    3. Ответственность
    <длинный body>

* Numbered list:
    1. сделать X
    2. сделать Y
    3. сделать Z

Признаки list (эвристика, **детерминированная**, без LLM):

1. ≥ 3 последовательных нумерованных блока (монотонные номера 1, 2, 3, ...).
2. Каждый блок короткий (≤ ``max_item_chars``, дефолт 200).
3. **Нет substantial body между ними** — каждый следующий блок
   идёт **сразу** после предыдущего (contiguous ordinals).

Используется ``structure/heading.py::apply_evidence_scoring`` для
дополнительного ``list_penalty`` к heading-кандидатам (защита от
micro-sections в длинных юр. документах со списками вида «1. … / 2. …»).

NOTE: regex'ы определены локально, чтобы избежать циклической
зависимости с ``heading.py`` (который импортирует ``list_detection``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from document.physical import (
    DocumentBlock,
)


_RE_NUMBERED_LEVEL_1 = re.compile(r"^\s*(\d+)\.\s+(.{2,200})$")
_RE_NUMBERED_LEVEL_2 = re.compile(r"^\s*(\d+)\.(\d+)\.?\s+(.{2,200})$")


@dataclass(frozen=True)
class ListDetectionConfig:
    """Параметры list-detection.

    ``max_item_chars`` увеличен со старого 200 до 600: для юридических
    документов (НК РФ, ГК РФ) средняя длина нумерованного пункта
    статьи 1.5К–7К символов. Старый порог 200 → ``is_list=False``
    для всех таких пунктов → list-penalty не применялся → каждый пункт
    ошибочно проходил как heading. 600 — компромисс: длинные legal-статьи
    не считаются list, но одиночный ambiguous run получает штраф через
    ``list_penalty_for_candidate``.
    """

    max_item_chars: int = 600
    min_run_length: int = 3
    body_threshold_chars: int = 200  # блок body ≥ этого размера «разрывает» list


@dataclass(frozen=True)
class ListRun:
    """Обнаруженная последовательность нумерованных блоков (list или section)."""

    start_ordinal: int
    end_ordinal: int
    block_ordinals: tuple[int, ...]
    numbers: tuple[int, ...]
    is_list: bool


def _parse_number(text: str) -> int | None:
    """Извлечь начальный номер из текста вида ``1. ...`` / ``1.2. ...``.

    Возвращает первую цифру из ведущего номера. None если не парсится.
    """
    s = text.strip()
    for regex in (_RE_NUMBERED_LEVEL_2, _RE_NUMBERED_LEVEL_1):
        m = regex.match(s)
        if m:
            try:
                return int(m.group(1))
            except (ValueError, IndexError):
                return None
    return None


def detect_list_runs(
    blocks: tuple[DocumentBlock, ...],
    config: ListDetectionConfig | None = None,
) -> list[ListRun]:
    """Найти все list-like runs нумерованных блоков в документе.

    Возвращает список ListRun. Каждый ListRun — это последовательность
    подряд идущих нумерованных блоков с монотонными номерами.

    Для каждого run определяется ``is_list``:

    * ``True`` если:
      - длина ≥ ``min_run_length`` (≥ 3);
      - каждый блок короткий (≤ ``max_item_chars``);
      - между блоками нет substantial body.

    * ``False`` иначе (раздел, одиночный блок, или разорванная цепочка).
    """
    cfg = config or ListDetectionConfig()

    runs: list[ListRun] = []
    current: list[tuple[int, int]] = []  # (ordinal, number)
    last_ordinal: int | None = None

    def _flush() -> None:
        nonlocal current
        if not current:
            return
        ords = tuple(o for o, _ in current)
        nums = tuple(n for _, n in current)
        is_list = _classify_run(ords, blocks, cfg)
        runs.append(
            ListRun(
                start_ordinal=ords[0],
                end_ordinal=ords[-1],
                block_ordinals=ords,
                numbers=nums,
                is_list=is_list,
            )
        )
        current = []

    for b in blocks:
        num = _parse_number(b.content)
        if num is None:
            _flush()
            last_ordinal = b.ordinal
            continue

        # contiguous? блок идёт сразу за предыдущим.
        if last_ordinal is not None and b.ordinal != last_ordinal + 1:
            _flush()
        elif current:
            prev_num = current[-1][1]
            if num != prev_num + 1:
                # не монотонно — закрываем run, начинаем новый с текущего.
                _flush()

        current.append((b.ordinal, num))
        last_ordinal = b.ordinal

    _flush()
    return runs


def _classify_run(
    ordinals: tuple[int, ...],
    blocks: tuple[DocumentBlock, ...],
    cfg: ListDetectionConfig,
) -> bool:
    """Определить, является ли последовательность list или section-цепочкой."""
    if len(ordinals) < cfg.min_run_length:
        return False

    by_ord = {b.ordinal: b for b in blocks}

    # Проверка 1: все блоки короткие.
    for o in ordinals:
        b = by_ord.get(o)
        if b is None:
            return False
        if len(b.content.strip()) > cfg.max_item_chars:
            return False

    # Проверка 2: между блоками нет substantial body.
    # ordinals монотонны (≥ step=1), и у нас уже contiguous run;
    # следовательно, между элементами нет других блоков вообще.
    # Но мы проверяем непрерывность ordinals:
    for i in range(len(ordinals) - 1):
        if ordinals[i + 1] != ordinals[i] + 1:
            return False

    return True


_NEIGHBOR_WINDOW = 10


def _neighbor_numbered_count(
    candidate_ordinal: int,
    list_runs: list[ListRun],
    *,
    window: int = _NEIGHBOR_WINDOW,
) -> int:
    """Сколько нумерованных блоков в окрестности ±window от кандидата.

    Используется для различения:

    * standalone heading (``1. Общие положения``) — в окрестности
      нет других нумерованных блоков → 0 соседей;
    * list item в нумерованной серии (``1. Содержание пункта 1``
      среди ``1./2./3.``) — есть несколько соседей → ≥ 1.
    """
    lo = candidate_ordinal - window
    hi = candidate_ordinal + window
    count = 0
    for run in list_runs:
        for o in run.block_ordinals:
            if o == candidate_ordinal:
                continue
            if lo <= o <= hi:
                count += 1
    return count


def list_penalty_for_candidate(
    candidate_ordinal: int,
    list_runs: list[ListRun],
) -> float:
    """Штраф к heading-score за попадание кандидата в list-run.

    Возвращает:

    * ``0.0`` если run длины 1 (standalone heading-кандидат, не list);
    * ``0.15`` если кандидат в **подтверждённом** list-run (≥ 5 элементов);
    * ``0.10`` если кандидат в **подтверждённом** коротком list (3–4);
    * ``0.08`` если кандидат в **ambiguous** run (run найден, но
      ``is_list=False``) длины ≥ 2 — это защита от ложных заголовков
      в длинных документах (НК РФ: 199 blocks, run обнаружен,
      ``is_list=False`` по ``max_item_chars``, но кандидат всё равно
      "голый" ``1. text``, и без legal marker это **почти наверняка
      не heading**).

    Этап 3 (план): одиночный run (длины 1) — это **не** list,
    а standalone heading-кандидат (``1. Общие положения`` в начале
    раздела). Штрафовать его за list-семантику — ломать реальные headings.

    Returns 0.0 если кандидат не входит ни в один run.
    """
    for run in list_runs:
        if candidate_ordinal not in run.block_ordinals:
            continue
        if len(run.block_ordinals) < 2:
            # Одиночный run. Если рядом есть другие нумерованные блоки —
            # часть серии, штрафуем (0.08). Если рядом никого — standalone
            # heading, не штрафуем.
            if _neighbor_numbered_count(candidate_ordinal, list_runs) == 0:
                return 0.0
            return 0.08
        if run.is_list:
            if len(run.block_ordinals) >= 5:
                return 0.15
            return 0.10
        # Ambiguous run (найден, но не классифицирован как list) —
        # наказываем голую десятичную нумерацию.
        return 0.08
    return 0.0


def ambiguous_decimal_penalty(candidate_ordinal: int, list_runs: list[ListRun]) -> float:
    """Доп. штраф для кандидатов с голой десятичной нумерацией в ambiguous run.

    Это **ещё одна** ступень защиты: даже если ``list_penalty_for_candidate``
    уже вернул 0.08, голая нумерация без legal marker / body должна
    быть почти запрещена.

    Условие применения (Этап 3 плана):

    * кандидат входит в run;
    * run **не** квалифицирован как ``is_list`` (т.е. ``_classify_run``
      вернул False — между блоками есть body или они длиннее
      ``max_item_chars``);
    * run содержит **минимум 2** нумерованных блока.

    Последнее условие важно: одиночный нумерованный блок (run длины 1)
    не является "ambiguous" — это просто standalone heading-кандидат,
    и штрафовать его за list-семантику неправильно (это бы сломало
    реальные heading'и вида ``1. Общие положения`` в начале раздела).

    Returns: 0.05 если кандидат в ambiguous run длины ≥ 2, иначе 0.0.
    """
    for run in list_runs:
        if (
            candidate_ordinal in run.block_ordinals
            and not run.is_list
            and len(run.block_ordinals) >= 2
        ):
            return 0.05
    return 0.0


def classify_ambiguous_run(
    ordinals: tuple[int, ...],
    blocks: tuple[DocumentBlock, ...],
    *,
    max_item_chars: int = 200,
) -> str:
    """Определить категорию для спорного numbered-run.

    PLAN §10 — различать heading vs list item в спорных случаях.

    Возвращает одно из:

    * ``"list"`` — явный list (короткие однотипные блоки).
    * ``"section"`` — heading'и (есть substantial body после).
    * ``"ambiguous"`` — нельзя определить детерминированно.
    """
    if len(ordinals) < 2:
        return "ambiguous"
    by_ord = {b.ordinal: b for b in blocks}
    short_count = 0
    for o in ordinals:
        b = by_ord.get(o)
        if b is None:
            continue
        if len(b.content.strip()) <= max_item_chars:
            short_count += 1
    if short_count == len(ordinals):
        return "list"
    if short_count == 0:
        return "section"
    return "ambiguous"


__all__ = [
    "ListDetectionConfig",
    "ListRun",
    "detect_list_runs",
    "list_penalty_for_candidate",
    "ambiguous_decimal_penalty",
    "classify_ambiguous_run",
]


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

from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
)


_RE_NUMBERED_LEVEL_1 = re.compile(r"^\s*(\d+)\.\s+(.{2,200})$")
_RE_NUMBERED_LEVEL_2 = re.compile(r"^\s*(\d+)\.(\d+)\.?\s+(.{2,200})$")


@dataclass(frozen=True)
class ListDetectionConfig:
    """Параметры list-detection."""

    max_item_chars: int = 200
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


def list_penalty_for_candidate(
    candidate_ordinal: int,
    list_runs: list[ListRun],
) -> float:
    """Штраф к heading-score за попадание кандидата в list-run.

    Возвращает 0.10 если кандидат находится внутри list-run, иначе 0.0.
    Дополнительно: если кандидат — единственный представитель номера
    в длинном run (≥ 5), он получает повышенный штраф (0.15),
    потому что это явный list.
    """
    for run in list_runs:
        if run.is_list and candidate_ordinal in run.block_ordinals:
            return 0.15 if len(run.block_ordinals) >= 5 else 0.10
    return 0.0


__all__ = [
    "ListDetectionConfig",
    "ListRun",
    "detect_list_runs",
    "list_penalty_for_candidate",
]

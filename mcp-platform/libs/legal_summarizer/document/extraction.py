"""Цепочка загрузки документа: извлечение → разделение → анализ.

Три стадии, и зависит от формата файла только первая:

1. **Извлечение** (:class:`TextExtractor` → :class:`ExtractionResult`) — формат
   отдаёт *сырые единицы*: текст в том виде, в каком он лежит в файле, с
   координатами. Ничего не решается про границы абзацев.
2. **Разделение** (:func:`split_units_into_blocks`) — **единственное** место,
   где решается, что является блоком. Правило одно для всех форматов:
   пустая строка — граница абзаца, а при её отсутствии — строка.
3. **Анализ** (``structure`` / ``chunking`` / ``planning``) — работает с
   ``DocumentBlock`` и о формате не знает.

Добавление нового типа файла цепочку не меняет: новый формат — это новый
класс в ``document/extractors/`` плюс одна регистрация в
``document/registry.py``. Ни разбиение, ни анализ, ни кеш об этом не знают.

Почему не «три загрузчика в одном модуле»: своя единица чтения на каждый
формат и есть тот дефект, который здесь снят. У PDF блоком была страница, у
DOCX — ``w:p``, у TXT — весь файл целиком, а детектор заголовков при этом
был один и тот же, и на TXT он не находил ничего. Пока единицу чтения
объявляет владелец формата, формат остаётся её владельцем. Теперь решение
принимает разделка, а извлечение только приносит текст.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from libs.legal_summarizer.document.physical import DocumentBlock

__all__ = [
    "ExtractionResult",
    "RawUnit",
    "TextExtractor",
    "render_table",
    "split_text_into_paragraphs",
    "split_units_into_blocks",
    "title_from_text",
]

#: Признак таблицы среди сырых единиц. Таблица атомарна и не разбирается
#: ни на одном формате — иначе разорванная таблица читалась бы как текст.
KIND_FLOW = "flow"
KIND_TABLE = "table"


@dataclass(frozen=True)
class RawUnit:
    """Порция текста в том виде, в каком она лежит в файле.

    ``RawUnit`` — ответ на вопрос «что файл отдал», а не «что считать
    блоком». Разделение принимает решение о границах само, и единственная
    причина разнести эти две вещи — чтобы новый формат не мог принести свою
    единицу чтения.

    Attributes:
        text: текст единицы целиком; границы внутри неё не разбирались.
        page_index: 1-based страница, если формат их знает (``None`` — если
            нет; выдумывать нумерацию страниц у .txt значило бы сообщить
            ложную точность, тем же доводом ранее снят fake pagination в
            DOCX).
        kind: ``"flow"`` — сплошной текст, ``"table"`` — таблица.
        paragraph_index: позиция абзаца в исходном документе (``None``, если
            формат абзацев не нумерует).
        table_index: позиция таблицы в исходном документе.
        block_metadata: мета единицы, переносимая в каждый её блок. По
            ``style`` из неё ``document.title`` выводит заголовок из
            форматирования DOCX.
    """

    text: str
    page_index: int | None = None
    kind: str = KIND_FLOW
    paragraph_index: int | None = None
    table_index: int | None = None
    block_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractionResult:
    """Результат стадии извлечения.

    Attributes:
        units: сырые единицы в document order.
        page_count: сколько страниц у документа по мнению формата. Формат
            без страниц объявляет ``1``, а не ``0``: ноль страниц у файла с
            текстом — это не факт, а дефект расчёта.
        title: заголовок из метаданных формата, если они есть. ``None`` —
            тогда заголовок выводится из первой содержательной строки
            (:func:`title_from_text`), то есть одинаково для всех форматов.
    """

    units: tuple[RawUnit, ...]
    page_count: int = 1
    title: str | None = None


@runtime_checkable
class TextExtractor(Protocol):
    """Стадия извлечения для одного формата файла.

    Извлекатель **не решает**, что является блоком. Его дело — отдать текст с
    координатами и, если формат умеет, заголовок из метаданных.
    """

    format_name: str

    def extract(self, path: Path) -> ExtractionResult:
        """Вернуть сырые единицы документа."""


# ──────────────────────────────────────────────────────────────────────
# Стадия 2: разделение. Единственное место, где принимается решение о блоке.
# ──────────────────────────────────────────────────────────────────────

_RE_BLANK_LINE = re.compile(r"\r?\n[ \t]*\r?\n")
_RE_NEWLINE = re.compile(r"\r?\n")


def split_text_into_paragraphs(text: str) -> list[str]:
    """Плоский текст → абзацы. Правило раздела, общее для всех форматов.

    Основной признак абзаца — пустая строка: её выдаёт и pypdf между
    абзацами, и человек, печатая текст в файл. Запасной путь — перевод
    строки, и он обязателен, а не украшение: текст без пустых строк — это
    обычный вывод ``pdf2txt`` и обычный результат «сохранить как .txt».
    Одним блоком такой текст доходил до детектора заголовков целиком, а
    детектор читает блок целиком и требует, чтобы заголовок стоял в его
    начале. Заголовок в начале документа не стоит никогда, поэтому структура
    схлопывалась в один корневой узел.

    Запасной путь включается только когда пустых строк нет вовсе: иначе
    абзац, просто написанный в несколько строк, был бы разорван на строки.
    """
    if not text.strip():
        return []
    paragraphs = [part.strip() for part in _RE_BLANK_LINE.split(text)]
    paragraphs = [part for part in paragraphs if part]
    if len(paragraphs) == 1:
        lines = [line.strip() for line in _RE_NEWLINE.split(paragraphs[0])]
        lines = [line for line in lines if line]
        if len(lines) > 1:
            return lines
    return paragraphs


def split_units_into_blocks(units: Iterable[RawUnit]) -> list[DocumentBlock]:
    """Сырые единицы → блоки документа.

    Здесь и только здесь решается, что является блоком, поэтому правило одно
    для всех форматов: у TXT не может быть «своего» блока, как не было своего
    блока у PDF. Формат задаёт координаты и вид единицы (текст или таблица),
    но не границы.

    Нумерация ``ordinal`` сквозная по всем единицам (invariant #3:
    ``blocks[i].ordinal == i``) — блоки разных единиц не должны
    перенумеровываться отдельно, иначе координаты внутри документа
    разъезжаются.
    """
    blocks: list[DocumentBlock] = []
    ordinal = 0
    for unit in units:
        is_table = unit.kind == KIND_TABLE
        if is_table:
            # Таблица атомарна: её строки нельзя разбирать на абзацы, и
            # детектор заголовков, и brief-контекст, и чанкер трактуют её
            # как единое целое.
            texts = [unit.text] if unit.text.strip() else []
        else:
            texts = split_text_into_paragraphs(unit.text)
        for text in texts:
            blocks.append(
                DocumentBlock(
                    block_id=f"b_{ordinal:04d}",
                    block_type=KIND_TABLE if is_table else "paragraph",
                    content=text,
                    char_count=len(text),
                    page_index=unit.page_index,
                    page_start=unit.page_index,
                    page_end=unit.page_index,
                    paragraph_index=unit.paragraph_index,
                    table_index=unit.table_index,
                    ordinal=ordinal,
                    block_metadata=dict(unit.block_metadata),
                )
            )
            ordinal += 1
    return blocks


# ──────────────────────────────────────────────────────────────────────
# Общее для всех форматов.
# ──────────────────────────────────────────────────────────────────────


def render_table(rows: Sequence[Sequence[str]]) -> str:
    """Таблица → текст с ``|``-разделителем ячеек (как в парсере)."""
    lines: list[str] = []
    for row in rows:
        cells = [c.strip() if c else "" for c in row]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def title_from_text(text: str) -> str | None:
    """Заголовок из текста: первая содержательная строка.

    Единое правило для всех форматов, у которых нет своих метаданных.
    Раньше оно лежало в ``_pick_title_from_text`` рядом с ветками по
    форматам, и ветки эти были не единственным источником заголовка: сам
    заголовок из метаданных тоже доставался формату, а не разделу.
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if len(line) >= 3:
            return line[:200]
    return None
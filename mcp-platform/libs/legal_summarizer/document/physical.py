"""PhysicalDocument: нормализованный список блоков документа с координатами.

Это **модель данных**, а не загрузчик. Как файл превращается в блоки —
вопрос цепочки ``извлечение → разделение → анализ``
(``document/extraction.py``, ``document/registry.py``,
``document/loader.py``), и формат файла участвует в ней ровно одной
стадией — извлечением.

Что берём у парсера (``libs.office``):
  * ``extract_text(path)`` → полный текст документа; ``title``,
    ``format``, ``size_bytes`` собираются из него точечными
    обёртками.

Граница между Physical и Semantic:

* ``PhysicalDocument`` описывает **только то, что физически есть в файле**
  — pages, paragraphs, tables. ``DocumentBlock`` — это physical unit.
* Семантическая структура (``heading``, ``list_item``, ``section`` и
  т.п.) — ответственность ``DocumentStructure`` из ``structure/models.py``,
  который **ссылается** на блоки через ordinals, а не копирует текст.
* ``PhysicalDocument`` намеренно **не знает** ни о каких ``semantic_type``,
  ``heading``, ``section``. Это строго отделено в ``structure/models.py``.
* ``DocumentBlock.block_type`` (``"paragraph"`` / ``"table"``)
  — это **physical** тип, не семантический. Значения ``"page"`` и ``"text"``
  больше не выдаются: **блок = абзац независимо от формата файла** —
  это решение стадии разделения, а не формата.

``DocumentIdentity`` — единственный owner identity/fingerprint
(``document/identity.py``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class DocumentBlock:
    """Один физический блок документа.

    Attributes:
        block_id: стабильный идентификатор вида ``"b_001"``.
        block_type: ``"paragraph"`` (любой формат — единая единица) |
            ``"table"`` | ``"slide"`` (PPTX,
            зарезервировано на будущее).
        content: текст блока.
        char_count: ``len(content)``.
        page_index: 1-based номер страницы (PDF) или страница, на которой
            находится параграф/таблица (DOCX).
        page_start / page_end: для multi-page block (будущее). Сейчас
            всегда равны ``page_index``.
        paragraph_index: индекс параграфа в DOCX (None для не-DOCX).
        table_index: индекс таблицы в DOCX/PDF (None для не-таблицы).
        ordinal: 0..N-1, canonical document order (invariant #3).
        block_metadata: дополнительная мета (например, ``{"row_count": 5}``
            для таблицы или ``{"style": "Heading 1"}`` для абзаца DOCX).
    """

    block_id: str
    block_type: str
    content: str
    char_count: int
    page_index: int | None
    page_start: int | None
    page_end: int | None
    paragraph_index: int | None
    table_index: int | None
    ordinal: int
    block_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PhysicalDocument:
    """Нормализованная физическая модель документа.

    Attributes:
        path: исходный путь.
        format: расширение файла (``pdf`` / ``docx`` / ``txt``).
        title: из метаданных формата либо из первой содержательной строки.
        size_bytes: размер файла.
        blocks: плоский список ``DocumentBlock`` в canonical document order.
            ``blocks[i].ordinal == i`` (invariant #3).
        page_count: сколько страниц у документа по мнению формата. Формат
            без страниц объявляет ``1``; ``0`` у файла с текстом означал бы
            не факт, а дефект расчёта.
    """

    path: str
    format: str
    title: str | None
    size_bytes: int
    blocks: tuple[DocumentBlock, ...]
    page_count: int
    _blocks_by_ord_cache: dict[int, DocumentBlock] | None = field(
        default=None, repr=False, compare=False,
    )

    @property
    def blocks_by_ord(self) -> dict[int, DocumentBlock]:
        """Lookup ``DocumentBlock`` по ``ordinal`` (identity, не position).

        Invariant в текущей реализации: ``blocks[i].ordinal == i``. Этот
        property всё равно работает через ``{b.ordinal: b}`` — это защищает
        callers (cache-assisted follow-up, context expansion) от поломки,
        если upstream когда-нибудь начнёт фильтровать/нормализовать
        blocks, оставляя ``ordinal`` как identity, но разрывая связь
        ``position == ordinal``.
        """
        cached = object.__getattribute__(self, "_blocks_by_ord_cache")
        if cached is not None:
            return cached
        mapping = {b.ordinal: b for b in self.blocks}
        object.__setattr__(self, "_blocks_by_ord_cache", mapping)
        return mapping

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "format": self.format,
            "title": self.title,
            "size_bytes": self.size_bytes,
            "page_count": self.page_count,
            "blocks": [b.to_dict() for b in self.blocks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PhysicalDocument:
        blocks = tuple(DocumentBlock(**b) for b in data["blocks"])
        return cls(
            path=data["path"],
            format=data["format"],
            title=data.get("title"),
            size_bytes=data["size_bytes"],
            blocks=blocks,
            page_count=data["page_count"],
        )


__all__ = [
    "DocumentBlock",
    "PhysicalDocument",
]
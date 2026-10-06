"""Цепочка ``извлечение → разделение → анализ`` и её расширение.

Формату принадлежит только первая стадия: извлечение отдаёт *сырые единицы*
и не решает, что является блоком. Разбиение и анализ — общие.

Проверяется:

1. Новый тип файла подключается регистрацией извлекателя, и блоки выходят
   те же, что у остальных форматов: правило раздела не трогали.
2. Новый формат не может принести свою гранулярность: на входе у раздела —
   ``RawUnit``, а не готовый блок.
3. Перечень форматов живёт в реестре, а не в ветвлении загрузчика.
4. Анализ (структура, секции, brief-чанк) даёт одинаковый результат для
   одного и того же текста в разных типах файлов.
"""

from __future__ import annotations

from pathlib import Path

from libs.legal_summarizer.document.extraction import (
    ExtractionResult,
    RawUnit,
    split_units_into_blocks,
)
from libs.legal_summarizer.document.loader import DocumentLoader
from libs.legal_summarizer.document.registry import (
    EXTRACTORS,
    get_extractor,
    register_extractor,
    supported_formats,
)

PARAGRAPHS = [
    "ГЛАВА 1",
    "Статья 1",
    "1. Российская Федерация есть демократическое правовое государство.",
    "Статья 2",
    "2. Признание и защита прав человека неотчуждаемы.",
]


class _StubExtractor:
    """Извлекатель нового формата: отдаёт текст и заголовок из метаданных."""

    format_name = "md"

    def extract(self, path: Path) -> ExtractionResult:
        return ExtractionResult(
            units=(RawUnit(text=path.read_text(encoding="utf-8")),),
            page_count=1,
            title="Заголовок из метаданных",
        )


def _register_stub() -> None:
    register_extractor("md", _StubExtractor())


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    with p.open("w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    return p


class TestNewFormatNeedsNoChainChange:
    """Добавление типа файла не меняет ни разбиение, ни анализ."""

    def test_new_format_gets_the_same_blocks(self, tmp_path: Path) -> None:
        _register_stub()
        body = "\n\n".join(PARAGRAPHS)
        md = _write(tmp_path, "doc.md", body)
        txt = _write(tmp_path, "doc.txt", body)

        from_md = DocumentLoader().load(md)
        from_txt = DocumentLoader().load(txt)

        assert from_md.format == "md"
        assert [b.content for b in from_md.blocks] == [b.content for b in from_txt.blocks]
        assert [b.content for b in from_md.blocks] == PARAGRAPHS

    def test_new_format_gets_the_line_fallback_too(self, tmp_path: Path) -> None:
        """Правило раздела общее, а не набор частных случаев по форматам."""
        _register_stub()
        md = _write(tmp_path, "doc.md", "\n".join(PARAGRAPHS))

        doc = DocumentLoader().load(md)

        assert [b.content for b in doc.blocks] == PARAGRAPHS

    def test_new_format_keeps_its_own_metadata_title(self, tmp_path: Path) -> None:
        _register_stub()
        md = _write(tmp_path, "doc.md", "\n\n".join(PARAGRAPHS))

        assert DocumentLoader().load(md).title == "Заголовок из метаданных"

    def test_format_without_metadata_falls_back_to_first_line(
        self, tmp_path: Path
    ) -> None:
        body = "Первая содержательная строка документа\n\nВторая"
        txt = _write(tmp_path, "doc.txt", body)

        assert DocumentLoader().load(txt).title == "Первая содержательная строка документа"


class TestExtractionStageCannotChooseGranularity:
    """Единица извлечения — ``RawUnit``, а не блок."""

    def test_raw_unit_has_no_field_for_its_own_granularity(self) -> None:
        """Формату некуда объявить, где кончается его абзац.

        Пока такого поля нет, «свой блок у своего формата» невыразимо в коде.
        Поле появится здесь — и дефект вернётся вместе с ним.
        """
        fields = set(RawUnit.__dataclass_fields__)

        assert fields == {
            "text",
            "page_index",
            "kind",
            "paragraph_index",
            "table_index",
            "block_metadata",
        }, f"у RawUnit появились поля: {sorted(fields)}"

    def test_shipped_extractors_put_only_coordinates_into_metadata(self) -> None:
        """Лазейки через ``block_metadata`` тоже нет.

        Свободный словарь мог бы стать тем же полем «своей гранулярности»,
        только с другим названием, поэтому проверяется содержимое: в мета
        извлекателей ходят только координаты оформления, а не решения о
        границах.
        """
        import inspect
        import re

        allowed = {"style", "row_count"}
        for fmt in sorted(supported_formats()):
            source = inspect.getsource(type(get_extractor(fmt)).extract)
            literals = re.findall(r"block_metadata=\{([^}]*)\}", source)
            keys = set(re.findall(r'"(\w+)"\s*:', " ".join(literals)))

            assert keys <= allowed, (
                f"извлекатель {fmt} кладёт в block_metadata ключи {sorted(keys - allowed)}; "
                "в мета допустимы только координаты оформления"
            )

    def test_txt_extractor_emits_raw_units(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "doc.txt", "первый\n\nвторой\n\nтретий")

        result = get_extractor("txt").extract(path)

        assert len(result.units) == 1, "txt обязан принести одну сырую единицу"
        assert isinstance(result.units[0], RawUnit)

    def test_splitting_is_the_only_place_that_splits(self) -> None:
        units = (RawUnit(text="раз\nдва\n\nтри"),)

        blocks = split_units_into_blocks(units)

        # Перевод строки внутри абзаца остаётся переводом строки: запасной
        # путь включается только когда пустых строк нет вовсе, иначе абзац,
        # просто написанный в несколько строк, рвался бы на строки.
        assert [b.content for b in blocks] == ["раз\nдва", "три"]
        assert [b.ordinal for b in blocks] == [0, 1]

    def test_line_fallback_only_when_no_blank_lines(self) -> None:
        units = (RawUnit(text="раз\nдва"),)

        blocks = split_units_into_blocks(units)

        assert [b.content for b in blocks] == ["раз", "два"]

    def test_table_unit_is_never_split(self) -> None:
        units = (RawUnit(text="а | б\nв | г", kind="table", table_index=3),)

        blocks = split_units_into_blocks(units)

        assert len(blocks) == 1
        assert blocks[0].block_type == "table"
        assert blocks[0].content == "а | б\nв | г"

    def test_unit_coordinates_reach_the_block(self) -> None:
        units = (
            RawUnit(text="текст страницы", page_index=7, block_metadata={"style": "H"}),
        )

        block = split_units_into_blocks(units)[0]

        assert block.page_index == 7
        assert block.page_start == 7 and block.page_end == 7
        assert block.block_metadata == {"style": "H"}


class TestFormatsComeFromRegistry:
    """Перечень форматов — реестр, а не ветвление в загрузчике."""

    def test_supported_formats_is_the_registry(self) -> None:
        assert supported_formats() == frozenset(EXTRACTORS)

    def test_unknown_format_is_refused_with_the_list(self) -> None:
        try:
            get_extractor("xyz")
        except ValueError as exc:
            message = str(exc)
            assert "xyz" in message
            assert "pdf" in message and "docx" in message and "txt" in message
        else:
            raise AssertionError("неизвестный формат не отвергнут")

    def test_register_rejects_a_non_extractor(self) -> None:
        try:
            register_extractor("bad", object())
        except TypeError:
            return
        raise AssertionError("объект без extract() принят как извлекатель")


class TestAnalysisIsFormatBlind:
    """Один и тот же текст — один и тот же разбор в любом типе файла."""

    def test_structure_and_brief_match_across_formats(self, tmp_path: Path) -> None:
        from libs.legal_summarizer.application import canonical
        from libs.legal_summarizer.application.brief_context import build_brief_chunk

        _register_stub()
        body = "\n\n".join(PARAGRAPHS)
        md = _write(tmp_path, "doc.md", body)
        txt = _write(tmp_path, "doc.txt", body)

        seen = []
        for path in (md, txt):
            insp = canonical.inspect_canonical(
                text="", document_path=path, workspace_root=tmp_path
            )
            brief = build_brief_chunk(insp.pipeline_result.analysis)
            seen.append(
                (
                    insp.structure.total_blocks,
                    len(insp.structure.nodes),
                    insp.strategy,
                    brief.char_count,
                )
            )

        assert seen[0] == seen[1], f"разбор зависит от типа файла: {seen}"
        assert seen[0][1] > 1, "структура схлопнулась в один корень"
        assert seen[0][3] > 0, "brief-чанк пуст"
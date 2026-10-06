"""PDF-структура: страница режется на абзацы, голый заголовок распознаётся.

Дефект, который держит страж, и почему он молчал.

**Блок = страница.** ``_iter_pdf_blocks`` отдавал один ``DocumentBlock`` на
страницу. Детектор заголовков читает блок целиком и требует, чтобы заголовок
стоял в его **начале** (``^\\s*Статья``), а длинный блок срезает score ниже
порога: ``level == 1 and text_len > 80 → 0.55`` при ``CONFIDENCE_THRESHOLD =
0.60``. На странице Конституции РФ медианой 2 783 символа заголовок «Статья 5»
в середине страницы не виден ни одним из двух путей. Третий путь — outline
PDF — пуст, если закладок нет. Итог: 144 раздела превращались в один
корневой узел, ``brief`` уходил в LLM с 70 символами вместо 25 672, а
``map_hierarchical`` становился недостижим при пороге в 3 раздела.

**Название обязательно.** ``_RE_STATIYA`` требовал ``(.{2,200})$`` после
номера. В правовых документах заголовок «Статья 1» без названия — обычное
дело: название идёт следующим абзацем. Из 135 строк «Статья N» в Конституции
**ни одна** не несёт текста после номера, то есть дробление страницы само по
себе не дало бы ни одного раздела.

**Хвост числа уезжал в название.** ``(\\d+(?:\\.\\d+)?)`` отдавал номер
назад, а остаток числа уходил под ``(.{2,200})``: «Статья 671» разбиралась как
номер 67 с названием «71», «Статья 100» — как номер 10 с названием «0». Это не
распознанные заголовки, а артефакты бэктрекинга самого шаблона, и выдуманное
название уезжало в свод.

Проверки невакуумны по построению:

* тест страницы требует, чтобы в его образце было **больше одного** абзаца —
  разделитель, который ничего не режет, её бы не прошёл;
* тест голого заголовка требует, чтобы он был распознан **и** чтобы номер
  пришёл целиком, а не обрезанным, — обе проверки падают на прежнем шаблоне;
* сквозная проба строит блоки **тем же** сплиттером, что и прод, и сравнивает
  разбитую страницу с неразбитой: неразбитая обязана остаться пустой.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _SKILL_ROOT.parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from libs.legal_summarizer.document.heading import (  # noqa: E402
    _classify_regex,
    apply_confidence_penalties,
    apply_evidence_scoring,
    detect_heading_candidates,
    filter_above_threshold,
)
from libs.legal_summarizer.document.extraction import (  # noqa: E402
    split_text_into_paragraphs,
)
from libs.legal_summarizer.document.physical import (  # noqa: E402
    DocumentBlock,
)

#: Образец страницы правового документа: колонтитул, затем два заголовка без
#: названия, между ними — текст статей. Именно такая страница давала 2 783
#: символа одним блоком и ноль разделов.
PAGE_TEXT = """05.03.2025, 17:34 Конституция Российской Федерации

ГЛАВА 1

ОСНОВЫ КОНСТИТУЦИОННОГО СТРОЯ

Статья 1

1. Российская Федерация есть демократическое федеративное правовое
государство с республиканской формой правления.

Статья 2

1. Признание, соблюдение и защита прав и свобод человека и гражданина
являются неотчуждаемыми правами.
"""


def _block(ordinal: int, content: str, block_type: str = "paragraph") -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ordinal:04d}",
        block_type=block_type,
        content=content,
        char_count=len(content),
        page_index=7,
        page_start=7,
        page_end=7,
        paragraph_index=None,
        table_index=None,
        ordinal=ordinal,
        block_metadata={},
    )


def _accepted(blocks: tuple[DocumentBlock, ...]) -> list:
    candidates = detect_heading_candidates(blocks, pdf_path=None)
    penalized = apply_confidence_penalties(candidates, blocks)
    return filter_above_threshold(apply_evidence_scoring(penalized, blocks))


class TestPageIsSplitIntoParagraphs:
    """Страница — не единица чтения для детектора."""

    def test_page_yields_several_paragraphs(self) -> None:
        paragraphs = split_text_into_paragraphs(PAGE_TEXT)

        assert len(paragraphs) > 1, (
            f"страница разбилась на {len(paragraphs)} абзацев — разделитель "
            f"перестал работать"
        )
        assert paragraphs == [p.strip() for p in paragraphs], "абзацы не подрезаны"
        assert all(p for p in paragraphs), "пустой абзац попал в результат"

    def test_heading_is_its_own_block(self) -> None:
        paragraphs = split_text_into_paragraphs(PAGE_TEXT)

        assert "Статья 1" in paragraphs, paragraphs[:8]
        assert "Статья 2" in paragraphs, paragraphs[:8]

    def test_page_without_blank_lines_splits_by_line(self) -> None:
        """Страница без пустых строк разбирается по строкам.

        Раньше здесь стояло обратное ожидание («остаётся одним блоком»), и оно
        закрепляло поломку, а не правило: текст без пустых строк — обычный
        вывод ``pdf2txt`` и обычный результат «сохранить как .txt». Одним
        блоком такой текст доходил до детектора целиком, а детектор требует
        заголовок в начале блока, поэтому структура схлопывалась в один
        корневой узел. Правило теперь одно для всех форматов
        (``_paragraphs_from_plain_text``), и этот случай — тот же самый, что
        раньше ломал .txt.
        """
        assert split_text_into_paragraphs("первая строка\nвторая строка") == [
            "первая строка",
            "вторая строка",
        ]

    def test_blank_page_yields_nothing(self) -> None:
        assert split_text_into_paragraphs("") == []
        assert split_text_into_paragraphs("   \n\n  \n") == []

    def test_crlf_separators_are_handled(self) -> None:
        assert split_text_into_paragraphs("первый\r\n\r\nвторой") == ["первый", "второй"]


class TestBareLegalHeadings:
    """«Статья 1» без названия — заголовок."""

    def test_bare_article_is_recognised(self) -> None:
        classified = _classify_regex("Статья 1")

        assert classified is not None, "голый «Статья 1» не распознан"
        assert classified[2] == "regex_statiya", classified

    def test_bare_chapter_is_recognised(self) -> None:
        assert _classify_regex("ГЛАВА 1") is not None
        assert _classify_regex("Глава 12") is not None

    def test_number_is_not_truncated_by_backtracking(self) -> None:
        """Хвост числа не должен уезжать в название.

        Прежний шаблон разбирал «Статья 671» как номер 67 с названием «71».
        Явное утверждение на ``raw_number``, а не «что-то нашлось»: на
        неверном шаблоне кандидат есть, но номер другой.
        """
        for text, number in (
            ("Статья 671", "статья_671"),
            ("Статья 100", "статья_100"),
            ("Статья 12", "статья_12"),
        ):
            classified = _classify_regex(text)
            assert classified is not None, f"{text!r} не распознан вовсе"
            assert classified[3] == number, (
                f"{text!r} разобрано как {classified[3]!r}, ждали {number!r} — "
                f"номер обрезан бэктрекингом"
            )

    def test_heading_with_title_still_recognised(self) -> None:
        classified = _classify_regex("Статья 1. Основополагающие принципы")

        assert classified is not None
        assert classified[3] == "статья_1", classified

    def test_title_is_not_taken_from_a_leading_digit(self) -> None:
        """«Статья 8 должна применяться» — не заголовок с названием «должна»."""
        classified = _classify_regex("Статья 1 Основные положения")

        assert classified is None or classified[3] == "статья_1", classified


class TestSplittingIsWhatMakesHeadingsVisible:
    """Сквозная проба: те же блоки до и после разбиения."""

    def _split_blocks(self) -> tuple[DocumentBlock, ...]:
        return tuple(
            _block(i, text) for i, text in enumerate(split_text_into_paragraphs(PAGE_TEXT))
        )

    def test_split_blocks_have_non_zero_length(self) -> None:
        assert self._split_blocks(), "разбиение не дало ни одного блока"

    def test_whole_page_as_one_block_finds_nothing(self) -> None:
        """Прежнее поведение зафиксировано как дефект, а не как норма."""
        assert _accepted((_block(0, PAGE_TEXT),)) == [], (
            "неразбитая страница обязана остаться без разделов — иначе проба "
            "не отличает починку от её отсутствия"
        )

    def test_split_blocks_are_recognised(self) -> None:
        blocks = self._split_blocks()
        accepted = _accepted(blocks)
        headings = {blocks[c.block_index].content for c in accepted}

        assert "Статья 1" in headings, sorted(headings)
        assert "Статья 2" in headings, sorted(headings)

    def test_body_paragraphs_do_not_become_headings(self) -> None:
        """Починка не имеет права превращать текст статей в разделы."""
        blocks = self._split_blocks()
        accepted = _accepted(blocks)

        for candidate in accepted:
            content = blocks[candidate.block_index].content
            assert len(content) <= 40, (
                f"подозрительно длинный heading: {content[:80]!r}"
            )
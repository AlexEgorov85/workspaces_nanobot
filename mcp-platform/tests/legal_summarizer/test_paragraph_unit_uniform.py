"""Единая единица блока: блок = абзац независимо от формата файла.

На три формата приходилось три единицы (страница у PDF, ``w:p`` у DOCX, весь
файл у TXT), и TXT-путь ломал структуру целиком: один блок на 138 тысяч
символов → ноль разделов → brief-чанк 0 символов, при том же тексте в PDF —
350 блоков и 144 раздела.

Проверяется:

1. Один и тот же текст в .txt и .docx даёт **одинаковые** блоки — правило
   общее, а не «у PDF и DOCX так, а TXT отдельно».
2. Текст без пустых строк тоже разбирается: это обычный вывод ``pdf2txt`` и
   обычный результат «сохранить как .txt».
3. ``block_type`` больше не ``"text"``: единая единица — единый тип.
4. Заголовки находятся в TXT-блоках (невакуумность: правило обязано давать
   структуру, а не просто больше блоков).
5. Snapshot старой версии — cache miss: правка формы блоков обязана доходить
   до уже разобранных документов, иначе выкатывается вслепую.
"""

from __future__ import annotations

import json
from pathlib import Path

PARAGRAPHS = [
    "ГЛАВА 1",
    "ОСНОВЫ КОНСТИТУЦИОННОГО СТРОЯ",
    "Статья 1",
    "1. Российская Федерация есть демократическое правовое государство.",
    "Статья 2",
    "2. Признание, соблюдение и защита прав и свобод человека неотчуждаемы.",
    "Статья 3",
    "3. Человек и его гражданин имеют основные права и свободы.",
]


def _write_txt(tmp_path: Path, body: str, name: str = "doc.txt") -> Path:
    # ``newline=""`` — писать байт в байт: иначе Windows переведёт ``\n`` в
    # ``\r\n`` и проба будет мерить не заявленный вход (ровно та ошибка, что
    # стоила двух прогонов при сборе границ блоков у DOCX).
    p = tmp_path / name
    with p.open("w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    return p


def _write_docx(tmp_path: Path, paragraphs: list[str], name: str = "doc.docx") -> Path:
    from docx import Document

    p = tmp_path / name
    doc = Document()
    for para in paragraphs:
        doc.add_paragraph(para)
    doc.save(str(p))
    return p


def _load(path: Path):
    from libs.legal_summarizer.document.loader import DocumentLoader

    return DocumentLoader().load(path)


class TestBlockUnitIsFormatIndependent:
    """Один текст — одинаковые блоки в любом контейнере."""

    def test_same_text_gives_same_blocks_in_txt_and_docx(self, tmp_path: Path) -> None:
        txt = _load(_write_txt(tmp_path, "\n\n".join(PARAGRAPHS)))
        docx = _load(_write_docx(tmp_path, PARAGRAPHS))

        assert txt.format == "txt" and docx.format == "docx"
        assert [b.content for b in txt.blocks] == [b.content for b in docx.blocks], (
            f"txt дал {len(txt.blocks)} блоков, docx — {len(docx.blocks)}; "
            "правило разбиения должно быть одним"
        )
        assert len(txt.blocks) == len(PARAGRAPHS), (
            f"ожидалось {len(PARAGRAPHS)} абзацев, получено {len(txt.blocks)}"
        )

    def test_single_newline_text_is_split(self, tmp_path: Path) -> None:
        """Нет пустых строк — не значит «один блок».

        Так выглядит вывод ``pdf2txt`` и результат «сохранить как .txt».
        Одним блоком такой файл доходил до детектора целиком.
        """
        doc = _load(_write_txt(tmp_path, "\n".join(PARAGRAPHS)))

        assert [b.content for b in doc.blocks] == PARAGRAPHS, (
            "текст без пустых строк обязан разбираться построчно, а не остаться "
            "одним блоком"
        )

    def test_blank_lines_take_precedence_over_lines(self, tmp_path: Path) -> None:
        """Пустая строка — основной признак абзаца, строка — запасной."""
        body = "первый абзац\nвторой абзац\n\nтретий абзац"
        doc = _load(_write_txt(tmp_path, body))

        assert [b.content for b in doc.blocks] == [
            "первый абзац\nвторой абзац",
            "третий абзац",
        ]

    def test_txt_blocks_are_paragraphs(self, tmp_path: Path) -> None:
        doc = _load(_write_txt(tmp_path, "\n\n".join(PARAGRAPHS)))

        types = {b.block_type for b in doc.blocks}
        assert types == {"paragraph"}, (
            f"у txt остался собственный тип блока: {types}. Единая единица "
            "обязана означать единый тип"
        )

    def test_ordinals_stay_contiguous(self, tmp_path: Path) -> None:
        doc = _load(_write_txt(tmp_path, "\n\n".join(PARAGRAPHS)))

        assert [b.ordinal for b in doc.blocks] == list(range(len(PARAGRAPHS)))

    def test_headings_are_found_in_txt_blocks(self, tmp_path: Path) -> None:
        """Невакуумность: блоков больше — значит ли заголовки находятся."""
        from libs.legal_summarizer.document.heading import (
            apply_confidence_penalties,
            apply_evidence_scoring,
            detect_heading_candidates,
            filter_above_threshold,
        )

        blocks = tuple(_load(_write_txt(tmp_path, "\n\n".join(PARAGRAPHS))).blocks)
        candidates = detect_heading_candidates(blocks, pdf_path=None)
        accepted = filter_above_threshold(
            apply_evidence_scoring(
                apply_confidence_penalties(candidates, blocks), blocks
            )
        )

        assert accepted, (
            "ни один заголовок не распознан — разделение на абзацы само по "
            "себе структуры не даёт"
        )
        assert sum(1 for c in accepted if c.text.startswith("Статья")) == 3, accepted


class TestTxtStructureIsNotFlat:
    """Сквозная проверка: разбор txt даёт структуру, а не один корень."""

    def test_structure_and_brief_chunk_are_not_empty(self, tmp_path: Path) -> None:
        from libs.legal_summarizer.application import canonical
        from libs.legal_summarizer.application.brief_context import build_brief_chunk

        path = _write_txt(tmp_path, "\n\n".join(PARAGRAPHS))
        insp = canonical.inspect_canonical(text="", document_path=path, workspace_root=tmp_path)

        assert insp.structure.total_blocks == len(PARAGRAPHS)
        assert len(insp.structure.nodes) > 1, (
            "структура схлопнулась в один корневой узел — это и был дефект "
            "формата txt"
        )
        brief = build_brief_chunk(insp.pipeline_result.analysis)
        assert brief.char_count > 0, (
            "brief-чанк пуст: модели нечего читать, кроме оглавления"
        )


class TestSnapshotVersionGating:
    """Правка формы блоков обязана быть видна на уже разобранном документе."""

    @staticmethod
    def _marker_dir(cache, document_id: str) -> Path:
        target = cache._document_dir(document_id)
        target.mkdir(parents=True, exist_ok=True)
        return target

    @staticmethod
    def _write_marker(cache, document_id: str, payload: str) -> None:
        target = TestSnapshotVersionGating._marker_dir(cache, document_id)
        (target / "_complete.marker").write_text(payload, encoding="utf-8")
        (target / "physical.json").write_text('{"blocks": []}', encoding="utf-8")
        (target / "analysis.json").write_text("{}", encoding="utf-8")

    def test_previous_version_marker_is_cache_miss(self, tmp_path: Path) -> None:
        from libs.legal_summarizer.cache.document_cache import DocumentCache

        cache = DocumentCache(tmp_path)
        self._write_marker(cache, "d_old", json.dumps({"version": 1, "completed_at": "x"}))

        assert cache.is_complete("d_old") is False, (
            "снимок старой версии выдан как пригодный — правка формы блоков "
            "не дойдёт до уже разобранного документа"
        )
        assert cache.read_snapshot("d_old") is None

    def test_current_version_marker_is_cache_hit(self, tmp_path: Path) -> None:
        from libs.legal_summarizer.cache.document_cache import (
            SNAPSHOT_VERSION,
            DocumentCache,
        )

        cache = DocumentCache(tmp_path)
        self._write_marker(
            cache, "d_new", json.dumps({"version": SNAPSHOT_VERSION, "completed_at": "x"})
        )

        assert cache.is_complete("d_new") is True

    def test_unreadable_marker_is_cache_miss(self, tmp_path: Path) -> None:
        from libs.legal_summarizer.cache.document_cache import DocumentCache

        cache = DocumentCache(tmp_path)
        self._write_marker(cache, "d_broken", "не json вовсе")

        assert cache.is_complete("d_broken") is False

    def test_old_snapshot_does_not_shadow_new_parse(self, tmp_path: Path) -> None:
        from libs.legal_summarizer.application.pipeline_structure import run_canonical_pipeline
        from libs.legal_summarizer.cache.document_cache import DocumentCache
        from libs.legal_summarizer.document.identity import DocumentIdentity

        path = _write_txt(tmp_path, "\n\n".join(PARAGRAPHS))
        document_id = DocumentIdentity.from_path(path).document_id

        cache = DocumentCache(tmp_path)
        target = self._marker_dir(cache, document_id)
        (target / "_complete.marker").write_text(
            json.dumps({"version": 1, "completed_at": "прежняя версия"}), encoding="utf-8"
        )
        (target / "physical.json").write_text(
            json.dumps({"blocks": [], "path": str(path)}), encoding="utf-8"
        )
        (target / "analysis.json").write_text("{}", encoding="utf-8")

        run_canonical_pipeline(path, workspace_root=tmp_path)

        snapshot = cache.read_snapshot(document_id)
        assert snapshot is not None
        physical = snapshot[0]
        assert len(physical["blocks"]) == len(PARAGRAPHS), (
            f"прочитан прежний снимок ({len(physical['blocks'])} блоков) вместо "
            "свежего разбора"
        )
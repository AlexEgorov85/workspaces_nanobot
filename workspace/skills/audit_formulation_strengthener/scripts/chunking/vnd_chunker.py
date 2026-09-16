"""Чанкование ВНД — обёртка над ``legal_summarizer``.

Не дублируем DocumentStructureChunker — используем публичный
``application.pipeline_structure.run_canonical_pipeline``, который
возвращает ``PipelineResult`` с готовыми ``Chunk``-ами для одного
документа.

Этот модуль — адаптер:

* принимает список путей к файлам ВНД (.pdf/.docx/.txt);
* для каждого вызывает ``run_canonical_pipeline``;
* склеивает результаты в плоский список ``VndChunk`` с указанием
  исходного файла.

Используется ``modes/search`` на этапе 5.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Добавляем корень репо и scripts/ legal_summarizer, чтобы импортировать
# run_canonical_pipeline без выставленного PYTHONPATH.
_SKILL_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = _SKILL_ROOT.parents[1]
_LS_SCRIPTS = _PROJECT_ROOT / "workspace" / "skills" / "legal_summarizer" / "scripts"
for _p in (str(_PROJECT_ROOT), str(_LS_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from chunking.chunks import Chunk  # type: ignore[import-not-found]  # noqa: E402


__all__ = ["VndChunk", "extract_vnd_chunks"]


@dataclass(frozen=True)
class VndChunk:
    """Чанк ВНД с указанием исходного файла.

    Attributes:
        source_file: путь к файлу-источнику (абсолютный или относительный
            от cwd).
        index: порядковый номер чанка в исходном файле (0-based).
        text: текст чанка.
        section_title: заголовок секции (если есть), иначе пустая строка.
        section_path: путь по дереву секций (например,
            ``"5. Сроки хранения / 5.4. ПДн"``), если есть.
        token_estimate: оценка числа токенов в чанке.
    """

    source_file: str
    index: int
    text: str
    section_title: str
    section_path: str
    token_estimate: int


def extract_vnd_chunks(
    vnd_paths: list[str],
    *,
    max_chunks_per_file: int | None = None,
) -> list[VndChunk]:
    """Извлечь чанки из всех ВНД-файлов.

    Для каждого пути вызывает ``run_canonical_pipeline`` (это I/O-bound
    операция: extract_text + structure build + chunking). Результаты
    склеиваются в плоский список с указанием исходного файла.

    Args:
        vnd_paths: список путей к файлам ВНД.
        max_chunks_per_file: safety net — ограничить число чанков на файл
            (полезно для очень больших ВНД; ``None`` = без лимита).

    Returns:
        Список ``VndChunk`` в порядке: сначала все чанки первого файла,
        затем второго и т.д. ``VndChunk.index`` — порядковый номер **внутри
        исходного файла**.

    Raises:
        FileNotFoundError: если какой-то путь не существует.
        RuntimeError: если ``run_canonical_pipeline`` не смог обработать файл
            (например, защищённый паролем PDF или скан без текстового слоя).
    """
    out: list[VndChunk] = []
    for path_str in vnd_paths:
        path = Path(path_str)
        if not path.exists():
            raise FileNotFoundError(f"Файл ВНД не найден: {path}")

        chunks = _chunk_one_file(path)
        if max_chunks_per_file is not None and len(chunks) > max_chunks_per_file:
            chunks = chunks[:max_chunks_per_file]

        for i, ch in enumerate(chunks):
            out.append(
                VndChunk(
                    source_file=str(path),
                    index=i,
                    text=ch.text,
                    section_title=_extract_section_title(ch),
                    section_path=_extract_section_path(ch),
                    token_estimate=int(getattr(ch, "token_estimate", 0) or 0),
                )
            )
    return out


def _chunk_one_file(path: Path) -> list[Chunk]:
    """Запустить canonical pipeline для одного файла ВНД.

    Raises:
        RuntimeError: при ошибке обработки.
    """
    try:
        from application.pipeline_structure import (  # type: ignore[import-not-found]
            run_canonical_pipeline,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Не удалось импортировать legal_summarizer.pipeline_structure. "
            "Убедитесь, что PYTHONPATH содержит корень репо, "
            "либо скрипт запускается через cli.py skill'а."
        ) from exc

    try:
        result = run_canonical_pipeline(path)
    except Exception as exc:
        raise RuntimeError(
            f"Не удалось обработать ВНД '{path}': {exc!r}. "
            "Возможно, файл защищён паролём или не содержит текстового слоя."
        ) from exc

    # PipelineResult имеет .analysis (DocumentAnalysis) со списком чанков.
    chunks: list[Chunk] = list(getattr(result.analysis, "chunks", []) or [])
    if not chunks:
        # Fallback: попробовать .chunks или просто .chunks
        chunks = list(getattr(result, "chunks", []) or [])
    return chunks


def _extract_section_title(chunk: Chunk) -> str:
    """Извлечь заголовок секции из Chunk (или пустую строку)."""
    section_ids = getattr(chunk, "section_ids", None) or []
    if not section_ids:
        return ""
    # Последний section_id — обычно самый глубокий заголовок.
    last = section_ids[-1]
    title = getattr(last, "title", None)
    return str(title) if title else ""


def _extract_section_path(chunk: Chunk) -> str:
    """Извлечь путь по дереву секций."""
    section_ids = getattr(chunk, "section_ids", None) or []
    if not section_ids:
        return ""
    titles: list[str] = []
    for s in section_ids:
        t = getattr(s, "title", None)
        if t:
            titles.append(str(t))
    return " / ".join(titles)


def estimate_vnd_size(vnd_paths: list[str]) -> dict[str, Any]:
    """Оценить размер ВНД без LLM-вызовов.

    Полезно для ``--estimate-only``: показывает аудитору, сколько чанков
    будет обработано и сколько примерно времени это займёт.

    Args:
        vnd_paths: список путей к файлам ВНД.

    Returns:
        dict с полями:
        - ``vnd_files``: int;
        - ``total_chars``: int (сумма длин всех извлечённых текстов);
        - ``total_pages_estimated``: int (грубая оценка через длину текста);
        - ``map_batches_planned``: int (число батчей для map-фазы;
          рассчитывается из оценки числа чанков и ``single_call_threshold``).
    """
    total_chars = 0
    for path_str in vnd_paths:
        path = Path(path_str)
        if not path.exists():
            continue
        try:
            # Используем load_text из legal_summarizer для извлечения
            # plain-text (быстрее, чем полный pipeline).
            from application.document_io import load_text  # type: ignore[import-not-found]

            text = load_text(path)
            total_chars += len(text)
        except Exception:  # noqa: BLE001
            # Не падаем в estimate — просто пропускаем проблемный файл.
            continue

    # Грубая оценка числа страниц: ~3000 символов на страницу A4.
    pages_est = max(1, total_chars // 3000)

    # Оценка числа чанков: исходим из chunk_size = 100K символов (default).
    chunks_est = max(1, total_chars // 100_000 + (1 if total_chars % 100_000 else 0))

    return {
        "vnd_files": len(vnd_paths),
        "total_chars": total_chars,
        "total_pages_estimated": pages_est,
        "chunks_estimated": chunks_est,
        "map_batches_planned": chunks_est,
    }

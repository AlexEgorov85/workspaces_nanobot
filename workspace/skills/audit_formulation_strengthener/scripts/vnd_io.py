"""Высокоуровневый I/O слой для ВНД: извлечение текста + чанкование + оценка.

* ``VndInputError`` — типизированное исключение для CLI;
* ``VndChunk`` — frozen-dataclass с метаданными чанка;
* ``VndBundle`` — результат ``prepare_vnd``: готовые чанки + ``size_estimate``;
* ``prepare_vnd`` — фасад: принимает список путей, извлекает текст через
  ``workspace.utils.office_files.extract_text``, чанкует через
  ``lib.services.text_splitter.split_text``.

Без секций: ``text_splitter.split_text`` не различает разделы документа.
Цитата в результатах поиска идентифицируется тройкой
``(source_file, chunk_index, text_excerpt)``.

SHA-256 ключ по ``violation + paths`` (прежний ``cache_key``) удалён —
был мёртвым полем, реальных кэш-провайдеров не использовал.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.core.skill_config import get_tool_config
from lib.services.text_splitter import split_text
from workspace.utils.office_files import extract_text

from workspace.skills.audit_formulation_strengthener.scripts.skill_config import (
    get_chunking_config,
)


_SKILL_NAME = "audit_formulation_strengthener"


__all__ = ["VndInputError", "VndChunk", "VndBundle", "prepare_vnd"]


class VndInputError(Exception):
    """Ошибка ввода ВНД: файл не найден, не читается, пустой и т.п.

    Attributes:
        error_type: машинно-читаемая категория (``no_vnd``,
            ``vnd_not_found``, ``vnd_unreadable``, ``vnd_empty``,
            ``too_many_chunks``).
        message: человекочитаемое сообщение.
        file: путь к проблемному файлу (если применимо).
    """

    def __init__(
        self,
        message: str,
        *,
        error_type: str = "vnd_input_error",
        file: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.file = file


@dataclass(frozen=True)
class VndChunk:
    """Чанк ВНД.

    Attributes:
        source_file: путь к файлу-источнику (как передан в CLI).
        index: глобальный порядковый номер чанка (0-based, по всем файлам).
        text: текст чанка.
    """

    source_file: str
    index: int
    text: str


@dataclass(frozen=True)
class VndBundle:
    """Подготовленный набор чанков ВНД.

    Attributes:
        vnd_paths: список исходных путей (для traceability).
        chunks: список ``VndChunk``.
        size_estimate: словарь ``{files, chunks_total, chunks_per_file, chars_total}``.
    """

    vnd_paths: list[str]
    chunks: list[VndChunk]
    size_estimate: dict[str, Any]


def prepare_vnd(
    vnd_paths: list[str],
    *,
    max_chunks: int | None = None,
) -> VndBundle:
    """Подготовить ВНД: извлечь текст, чанковать, оценить размер.

    Args:
        vnd_paths: список путей к файлам ВНД (.pdf/.docx/.txt).
        max_chunks: явное ограничение суммарного числа чанков. Если None —
            берётся из ``project.json::skills.audit_formulation_strengthener.
            execution.max_chunks_for_execution``. Если None и в конфиге нет —
            без ограничения.

    Returns:
        ``VndBundle`` с готовыми чанками и ``size_estimate``.

    Raises:
        VndInputError: при любой ошибке ввода.
    """
    if not vnd_paths:
        raise VndInputError("Не указаны файлы ВНД", error_type="no_vnd")

    # 1. Проверка существования всех файлов.
    missing = [p for p in vnd_paths if not Path(p).exists()]
    if missing:
        raise VndInputError(
            f"Не найдены файлы ВНД: {', '.join(missing)}",
            error_type="vnd_not_found",
        )

    # 2. Извлечение текста и чанкование.
    # chunk_size / chunk_overlap — строго из project.json через
    # lib.core.skill_config.get_chunking_config. Дефолты там же (если
    # ключ не задан в project.json — fallback в lib, не здесь).
    chunking = get_chunking_config()
    chunk_size = int(chunking["chunk_size"])
    chunk_overlap = int(chunking["chunk_overlap"])

    chunks: list[VndChunk] = []
    chunks_per_file: list[int] = []
    chars_total = 0
    global_index = 0

    for path_str in vnd_paths:
        path = Path(path_str)
        # 2a. Извлечь текст в try/except — любое исключение → vnd_unreadable.
        try:
            text = extract_text(path)
        except FileNotFoundError as exc:
            # Защита от race condition: файл удалён между exists() и extract.
            raise VndInputError(
                f"Файл ВНД исчез во время обработки: {path}",
                error_type="vnd_not_found",
                file=str(path),
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise VndInputError(
                f"Не удалось прочитать файл ВНД '{path}': {exc!r}",
                error_type="vnd_unreadable",
                file=str(path),
            ) from exc

        # 2b. Пустой текст → vnd_empty с именем файла (явная ошибка,
        #     не тихий пропуск — критично для аудитора).
        if not text or not text.strip():
            raise VndInputError(
                f"Файл ВНД '{path}' не содержит текста "
                f"(скан без OCR, битый файл или пустой).",
                error_type="vnd_empty",
                file=str(path),
            )

        # 2c. Чанкование.
        file_chunks_text = split_text(
            text, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
        )

        per_file_count = 0
        for chunk_text in file_chunks_text:
            chunks.append(
                VndChunk(source_file=str(path), index=global_index, text=chunk_text)
            )
            global_index += 1
            per_file_count += 1
        chunks_per_file.append(per_file_count)
        chars_total += len(text)

    # 3. Лимит на суммарное число чанков (safety net из project.json:
    #     execution.max_chunks_for_execution).
    if max_chunks is None:
        tool_cfg = get_tool_config(_SKILL_NAME)
        configured = tool_cfg.get("execution", {}).get("max_chunks_for_execution")
        if configured is not None:
            max_chunks = int(configured)

    if max_chunks is not None and len(chunks) > max_chunks:
        raise VndInputError(
            f"Слишком много чанков: {len(chunks)} > {max_chunks} "
            f"(лимит execution.max_chunks_for_execution). "
            f"Уменьшите число ВНД или увеличьте chunk_size.",
            error_type="too_many_chunks",
        )

    # 4. size_estimate.
    size_estimate: dict[str, Any] = {
        "files": len(vnd_paths),
        "chunks_total": len(chunks),
        "chunks_per_file": chunks_per_file,
        "chars_total": chars_total,
    }

    return VndBundle(
        vnd_paths=list(vnd_paths),
        chunks=chunks,
        size_estimate=size_estimate,
    )

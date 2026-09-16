"""Высокоуровневый I/O слой для ВНД.

Содержит:

* ``VndInputError`` — типизированное исключение для CLI;
* ``prepare_vnd`` — фасад: принимает список путей, возвращает
  структурированный набор чанков + оценку размера + cache-key.

Используется ``modes/search`` и ``modes/synthesize``. Не используется
``modes/analyze`` (нормализация отклонения не требует чтения ВНД).
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Добавляем пути в sys.path для импорта.
_SKILL_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
_REPO_ROOT = _SKILL_ROOT.parents[2]
_LS_SCRIPTS = str(_REPO_ROOT / "workspace" / "skills" / "legal_summarizer" / "scripts")

# Добавляем пути
_paths = [str(_SCRIPTS_DIR), str(_REPO_ROOT), _LS_SCRIPTS]
for _p in _paths:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vnd_chunking import vnd_chunker  # type: ignore[import-not-found]  # noqa: E402


__all__ = ["VndInputError", "VndBundle", "prepare_vnd", "build_cache_key"]


class VndInputError(Exception):
    """Ошибка ввода ВНД: файл не найден, не читается и т.п.

    Attributes:
        error_type: машинно-читаемая категория (например,
            ``"vnd_not_found"``, ``"vnd_unreadable"``).
        message: человекочитаемое сообщение.
    """

    def __init__(self, message: str, error_type: str = "vnd_input_error") -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


@dataclass(frozen=True)
class VndBundle:
    """Подготовленный набор чанков ВНД.

    Attributes:
        vnd_paths: список исходных путей (для traceability в отчёте).
        chunks: список ``VndChunk``.
        cache_key: детерминированный ключ для кэширования
            (SHA-256 от violation + sorted(vnd_paths)).
        size_estimate: оценка размера (см. ``estimate_vnd_size``).
    """

    vnd_paths: list[str]
    chunks: list[Any]  # VndChunk из vnd_chunker
    cache_key: str
    size_estimate: dict[str, Any]


def prepare_vnd(
    vnd_paths: list[str],
    *,
    violation: str = "",
    max_chunks_per_file: int | None = None,
) -> VndBundle:
    """Подготовить ВНД: извлечь чанки, оценить размер, построить cache_key.

    Args:
        vnd_paths: список путей к файлам ВНД.
        violation: текст отклонения (используется в cache_key;
            опционально, чтобы можно было строить кэш и без violation).
        max_chunks_per_file: ограничение числа чанков на файл.

    Returns:
        ``VndBundle`` с готовыми чанками и метаданными.

    Raises:
        VndInputError: при ошибках ввода (файл не найден / не читается).
    """
    if not vnd_paths:
        raise VndInputError("Не указаны файлы ВНД", error_type="no_vnd")

    # 1. Проверка существования.
    missing: list[str] = [p for p in vnd_paths if not Path(p).exists()]
    if missing:
        raise VndInputError(
            f"Не найдены файлы ВНД: {', '.join(missing)}",
            error_type="vnd_not_found",
        )

    # 2. Извлечение чанков.
    try:
        chunks = vnd_chunker.extract_vnd_chunks(
            vnd_paths, max_chunks_per_file=max_chunks_per_file
        )
    except FileNotFoundError as exc:
        raise VndInputError(str(exc), error_type="vnd_not_found") from exc
    except RuntimeError as exc:
        raise VndInputError(str(exc), error_type="vnd_unreadable") from exc

    if not chunks:
        raise VndInputError(
            "Не удалось извлечь ни одного чанка из ВНД. "
            "Возможно, файлы пусты или не содержат текстового слоя.",
            error_type="vnd_empty",
        )

    # 3. Оценка размера (для --estimate-only и пользовательского UI).
    size_estimate = vnd_chunker.estimate_vnd_size(vnd_paths)

    # 4. Cache key.
    cache_key = build_cache_key(violation=violation, vnd_paths=vnd_paths)

    return VndBundle(
        vnd_paths=list(vnd_paths),
        chunks=chunks,
        cache_key=cache_key,
        size_estimate=size_estimate,
    )


def build_cache_key(*, violation: str, vnd_paths: list[str]) -> str:
    """Построить детерминированный ключ кэша.

    SHA-256 от ``violation + sorted(vnd_paths)``. Один и тот же набор
    ВНД + одна и та же формулировка → один cache_key → можно
    переиспользовать результат analyze/search.

    Args:
        violation: текст отклонения.
        vnd_paths: список путей к ВНД.

    Returns:
        64-char hex string.
    """
    h = hashlib.sha256()
    h.update(violation.encode("utf-8", errors="replace"))
    for p in sorted(vnd_paths):
        h.update(b"\x00")
        h.update(p.encode("utf-8", errors="replace"))
    return h.hexdigest()

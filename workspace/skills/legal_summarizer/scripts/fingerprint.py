"""Document fingerprint + session key resolution.

Функции:
    * ``fingerprint_file(path)`` — sha256 от полного содержимого файла,
      обрезанный до 16 hex символов (для совместимости с длиной legacy
      ``document_id_for(text)``). Используется как предпочтительный
      document_id, когда путь валиден.
    * ``document_id_for(text)`` — legacy: sha256 от первых 64KB текста.
      Используется как fallback, когда путь к файлу недоступен
      (например, для inline-документов без ``document_path``).
    * ``resolve_session_key(document_path)`` — извлечь ``safe_session_key``
      из пути документа (``None`` если путь не содержит
      ``data_store/cache/sessions/<key>/``).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from workspace.utils.session_key import (
    extract_session_key_from_path,
    safe_session_key,
)


def fingerprint_file(path: str | Path) -> str:
    """sha256 от полного содержимого файла (обрезанный до 16 hex).

    Преимущество перед ``document_id_for(text)``: устойчив к изменениям
    в «хвосте» документа (например, добавление страниц в конец PDF),
    которые не меняют первый 64KB, но меняют sha256 файла.

    Args:
        path: путь к файлу. Должен существовать.

    Returns:
        16-символьный hex sha256.

    Raises:
        FileNotFoundError: файл не существует.
        OSError: ошибка чтения.
    """
    digest = hashlib.sha256()
    p = Path(path)
    with p.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()[:16]


def document_id_for(text: str) -> str:
    """Legacy document_id = sha256 от первых 64KB текста.

    Сохранено для обратной совместимости (используется как fallback
    в :func:`resolve_document_id`, когда путь недоступен).

    NOTE: этот метод **менее устойчив**, чем ``fingerprint_file`` —
    одинаковый id для разных файлов с одинаковым первым 64KB. Для
    новых вызовов предпочитать ``fingerprint_file``.
    """
    sample = text[: 64 * 1024].encode("utf-8", errors="replace")
    return hashlib.sha256(sample).hexdigest()[:16]


def resolve_session_key(document_path: str | None) -> str | None:
    """Извлечь safe_session_key из пути документа.

    Идентично SessionFileRedirectHook: raw → safe_session_key() → str.
    ``None`` если путь не содержит ``data_store/cache/sessions/<key>/``.
    """
    if not document_path:
        return None
    raw = extract_session_key_from_path(document_path)
    if not raw:
        return None
    return safe_session_key(raw)


def resolve_document_id(document_path: str | Path | None, text: str) -> str:
    """Предпочтительный document_id для ``run()``.

    Возвращает ``fingerprint_file(document_path)``, если путь валиден
    и файл существует. Иначе — ``document_id_for(text)``.

    Это сделано, чтобы:
        * прогон одного и того же файла всегда давал один document_id
          (даже если текст извлечён по-разному из-за edge-кейсов OCR);
        * одинаковый префикс 64KB при разных хвостах давал разные id.
    """
    if document_path:
        try:
            return fingerprint_file(document_path)
        except (FileNotFoundError, OSError):
            pass
    return document_id_for(text)


__all__ = [
    "fingerprint_file",
    "document_id_for",
    "resolve_session_key",
    "resolve_document_id",
]


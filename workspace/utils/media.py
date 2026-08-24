"""Media-кодек — единая точка работы с вложениями сообщений (поле ``media``).

Разные каналы и web-UI обмениваются вложениями, и чтобы схема жила в одном
месте (а не копировалась в ``postgres_channel``, ``redis_channel`` и
``streamlit_app``), весь сериализатор/десериализатор вынесен сюда.

Форматы ``media``:

  runtime (контракт агента)      — ``list[str]``: локальные пути и/или URL
                                    (в таком виде заполняется телеграм-каналом,
                                    redis-каналом и т.д.).
  storage (колонка/поле в БД)     — ``list[dict]`` AW-совместимой схемы
                                    ``{"filename", "file_id", "mime_type",
                                    "file_size"}``. ``file_id`` = data URL или
                                    исходный путь/URL.
  legacy storage (только чтение)  — ``{"filename", "data"}`` и
                                    ``{"filename", "path"}`` терпим на чтении.

Пользователи кодекса:
  * ``postgres_channel`` — embеd (send) и decode (poll);
  * ``redis_channel`` — то же самое поверх Redis-очередей;
  * ``streamlit_app`` — запись user-медиа в DB и отрисовка вложений.
"""

from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path
from typing import Any

from loguru import logger

# Каноническая схема одного вложения в storage-формате.
# Ключи, которые AW (audit_point_new) читает в map_answer_to_blocks.
_STORAGE_KEYS = ("filename", "file_id", "mime_type", "file_size")


def data_url_info(data_url: str) -> tuple[str, int] | None:
    """Достать ``(mime_type, file_size)`` из ``data:<mime>;base64,<payload>``.

    Возвращает ``None`` для не-``data:``-URL или при ошибке декодирования.
    """
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    m = re.match(r"^data:([^;,]+)(?:;[^,]*)*;base64,(.+)$", data_url, re.DOTALL)
    if not m:
        return None
    mime = m.group(1).strip().lower()
    if not mime:
        mime = "application/octet-stream"
    try:
        size = len(base64.b64decode(m.group(2), validate=False))
    except Exception:
        size = 0
    return mime, size


def _storage_entry(
    data_url: str, filename: str, mime_type: str, file_size: int
) -> dict[str, Any]:
    """Собрать один storage-элемент AW-схемы."""
    return {
        "filename": filename,
        "file_id": data_url,
        "mime_type": mime_type,
        "file_size": file_size,
    }


def entry_from_data_url(data_url: str, filename: str | None = None) -> dict[str, Any]:
    """Собрать storage-элемент из готового data URL.

    Имя по умолчанию выводится из MIME-типа (``file.png``); при переданном
    ``filename`` сохраняется оригинальное имя (для user-upload из streamlit).
    """
    info = data_url_info(data_url)
    mime_type = info[0] if info else "application/octet-stream"
    file_size = info[1] if info else 0
    if not filename:
        ext = mimetypes.guess_extension(mime_type) or ""
        filename = f"file{ext}" if ext else "file"
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        mime_type, file_size = "", 0
    return _storage_entry(data_url, filename, mime_type, file_size)


def serialize(media: list[str]) -> list[Any]:
    """Превратить runtime ``list[str]`` (пути/URL) в storage AW-дикты.

    Для локальных файлов читается содержимое и кодируется как data URL;
    для готовых data URL восстанавливаются mime/размер; для HTTP/S — ссылка
    кладётся в ``file_id`` без превью.
    ``None``/пустой список возвращаются как есть (для идемпотентности).
    """
    if not media:
        return media
    embedded: list[Any] = []
    for path in media:
        if not isinstance(path, str) or not path:
            continue
        if path.startswith("data:"):
            embedded.append(entry_from_data_url(path))
            continue
        if path.startswith(("http://", "https://")):
            embedded.append(_storage_entry(path, "", "", 0))
            continue
        try:
            p = Path(path).expanduser()
            if not p.is_file():
                logger.warning("Media file not found, keeping path: {}", path)
                embedded.append(_storage_entry(path, p.name or Path(path).name, "", 0))
                continue
            raw = p.read_bytes()
            mime_type, _ = mimetypes.guess_type(str(p))
            if not mime_type:
                mime_type = "application/octet-stream"
            b64 = base64.b64encode(raw).decode("ascii")
            embedded.append(
                _storage_entry(
                    f"data:{mime_type};base64,{b64}",
                    p.name,
                    mime_type,
                    len(raw),
                )
            )
        except Exception:
            logger.exception("Failed to encode media file: {}", path)
            embedded.append(_storage_entry(path, Path(path).name, "", 0))
    return embedded


def deserialize(
    media: list[Any],
    file_store: Any,
    session_key: str = "default",
) -> list[Any]:
    """Перевести storage-медиа обратно в runtime-формат для агента.

    data URL (в ``file_id``/``data``/строке) сохраняются через
    ``SessionFileStore.save_attachment`` и возвращаются как ``dict``
    ``{"filename", "path"}`` или строка-путь; внешние ссылки и локальные
    пути проходят как есть.
    """
    if not media:
        return media
    resolved: list[Any] = []
    for entry in media:
        try:
            if isinstance(entry, dict):
                data = (
                    entry.get("file_id")
                    or entry.get("data")
                    or entry.get("path")
                    or ""
                )
                if not isinstance(data, str) or not data.startswith("data:"):
                    resolved.append(entry)
                    continue
                info = file_store.save_attachment(
                    session_key, data, filename=entry.get("filename") or None,
                )
                if not info:
                    resolved.append(entry)
                    continue
                resolved.append({"filename": info["filename"], "path": info["path"]})
                continue
            if not isinstance(entry, str) or not entry.startswith("data:"):
                resolved.append(entry)
                continue
            info = file_store.save_attachment(session_key, entry)
            if not info:
                resolved.append(entry)
                continue
            resolved.append(info["path"])
        except Exception:
            logger.exception("Failed to decode media data URL")
            resolved.append(entry)
    return resolved


def resolve_paths_and_hints(media: list[Any]) -> tuple[list[str], list[str]]:
    """Из декодированных media (строки-пути или dict filename/path) извлечь
    пути для агента и подсказки «файл лежит там-то»."""
    media_paths: list[str] = []
    hints: list[str] = []
    for entry in media:
        if isinstance(entry, dict):
            path = str(entry.get("path") or "")
            name = str(entry.get("filename") or (Path(path).name if path else ""))
        else:
            path = str(entry) if entry else ""
            name = Path(path).name if path else ""
        if not path:
            continue
        media_paths.append(path)
        if name:
            hints.append(f"[Attachment: {name} (saved at {path})]")
        else:
            hints.append(f"[Attachment: saved at {path}]")
    return media_paths, hints


def normalize_storage_entry(entry: Any) -> Any:
    """Нормализовать один legacy-элемент media в AW-формат (для backfill).

    ``dict {"filename", "data": "data:..."}`` → ``{"filename", "file_id",
    "mime_type", "file_size"}`` (mime/размер выводятся из data URL).
    Всё остальное (dict с ``file_id``, dict с ``path``, строки-URL/пути)
    возвращается без изменений — идемпотентно.
    """
    if not isinstance(entry, dict):
        return entry
    data = entry.get("data")
    if not (isinstance(data, str) and data.startswith("data:")):
        return entry
    if entry.get("file_id"):
        return entry
    return entry_from_data_url(data, entry.get("filename") or None)


def read_for_ui(entry: Any) -> tuple[str, str, str]:
    """Толерантный читатель любого спорный shape для отрисовки.

    Возвращает ``(data_url, path, filename)``, пробуя ``file_id`` (новый
    AW), ``data`` (legacy), ``path`` (после decode) и строковые media.
    Позволяет UI (streamlit) не знать ни одной из схем.
    """
    if not isinstance(entry, dict):
        s = entry if isinstance(entry, str) else ""
        if s.startswith("data:"):
            return s, "", "file"
        return "", s, Path(s).name or "file"

    filename = entry.get("filename") or "file"
    fid = entry.get("file_id")
    if isinstance(fid, str) and fid.startswith("data:"):
        return fid, "", filename
    legacy_data = entry.get("data")
    if isinstance(legacy_data, str) and legacy_data.startswith("data:"):
        return legacy_data, "", filename
    path = entry.get("path") or ""
    if isinstance(fid, str) and fid.startswith(("http://", "https://")):
        if not path:
            path = fid
    return "", path, filename


__all__ = [
    "_STORAGE_KEYS",
    "data_url_info",
    "entry_from_data_url",
    "serialize",
    "deserialize",
    "resolve_paths_and_hints",
    "read_for_ui",
    "normalize_storage_entry",
]

"""Document cache: per-session chunk-results для follow-up вопросов.

Контракт:
    * Cache живёт в ``workspace/data_store/cache/sessions/<session_key>/
      skills/legal_summarizer/documents/<document_id>/chunks/``.
    * Включается только если путь к файлу содержит session-папку
      (``SessionFileRedirectHook`` convention).
    * Используется для пропуска map-фазы при повторных вопросах к тому
      же документу в той же сессии (экономия 3-5 минут).
    * Каждая запись содержит provenance
      (``block_indices``, ``source_char_start/end``, ``block_types``,
      ``table_id/row_*``, ``chunk_text_preview``). ``meta.json`` хранит
      ``physical_cache_key`` (sha256 от файла) для проверки свежести
      при follow-up retrieval.

Очистка кэша — политика ``SessionFileRedirectHook`` (удаляется
вместе с папкой сессии).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def doc_cache_dir(document_id: str, session_key: str, workspace_root: Path) -> Path:
    """Путь к document-cache для ``(document_id, session_key)``."""
    return (
        workspace_root
        / "data_store" / "cache" / "sessions" / session_key
        / "skills" / "legal_summarizer" / "documents" / document_id
    )


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _safe_fingerprint(path: str | Path | None) -> str | None:
    """sha256 от файла (обрезанный). None если недоступен.

    Используется как ``physical_cache_key`` для проверки свежести cache:
    если sha256 между сохранением и retrieve не совпадает → cache stale
    → не использовать provenance.
    """
    if not path:
        return None
    try:
        from workspace.skills.legal_summarizer.scripts.fingerprint import (
            fingerprint_file,
        )
        return fingerprint_file(path)
    except (FileNotFoundError, OSError):
        return None


def load_doc_cache(
    document_id: str,
    session_key: str,
    workspace_root: Path,
) -> dict[str, dict]:
    """Загрузить chunks из document-cache. ``{chunk_id: chunk_data}``.

    Загружает как есть — новые provenance-поля (``block_indices``,
    ``source_char_*``, ``table_*``, ``chunk_text_preview``) присутствуют
    как обычные ключи. Старые cache без них ломаются downstream-логикой
    retrieval — не здесь.
    """
    cache = doc_cache_dir(document_id, session_key, workspace_root)
    chunks_dir = cache / "chunks"
    if not chunks_dir.is_dir():
        return {}
    out: dict[str, dict] = {}
    for f in chunks_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cid = data.get("chunk_id") or f.stem
        out[cid] = data
    return out


def load_doc_cache_meta(
    document_id: str,
    session_key: str,
    workspace_root: Path,
) -> dict[str, Any] | None:
    """Прочитать ``meta.json``.

    Возвращает ``None``, если meta нет. Содержит как минимум:
        * ``document_id``
        * ``first_seen_at``
        * ``physical_cache_key`` (sha256 файла; для freshness-check)
    """
    meta_path = doc_cache_dir(document_id, session_key, workspace_root) / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def cache_is_fresh(
    document_id: str,
    session_key: str,
    workspace_root: Path,
    document_path: str | Path | None,
) -> bool:
    """Проверить, что PhysicalDocument не изменился с момента кэширования.

        * Если cache для документа не существует → True (нет cache, нечего
          признать stale).
        * Если cache есть, но без ``physical_cache_key`` → True (legacy,
          нельзя проверить, считаем свежим — back-compat).
        * Если ``physical_cache_key`` в meta совпадает с текущим
          fingerprint файла → True (свежий).
        * Иначе → False (stale → не использовать provenance).

    Args:
        document_path: текущий путь к PhysicalDocument. Если None — cache
            считается «свежим» (нет способа проверить).
    """
    meta = load_doc_cache_meta(document_id, session_key, workspace_root)
    if meta is None:
        return True
    cached_key = meta.get("physical_cache_key")
    if not cached_key:
        return True
    current_key = _safe_fingerprint(document_path)
    if current_key is None:
        return True
    return cached_key == current_key


def save_doc_cache(
    document_id: str,
    session_key: str,
    workspace_root: Path,
    new_chunks: dict[str, dict],
    *,
    progress: Any = None,
    document_path: str | Path | None = None,
) -> None:
    """Сохранить новые chunks в document-cache (атомарно по файлу).

    Args:
        progress: callable(str) для вывода ошибки сохранения. Если
            ``None`` — fallback на ``print(..., file=sys.stderr)``.
            Сохранёнено для совместимости с ``summarizer._progress``.
        document_path: путь к исходному файлу (для ``physical_cache_key``
            в ``meta.json``). Если None — key не вычисляется.
    """
    if not new_chunks:
        return
    cache = doc_cache_dir(document_id, session_key, workspace_root)
    chunks_dir = cache / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    meta_path = cache / "meta.json"
    if not meta_path.exists():
        try:
            payload: dict[str, Any] = {
                "document_id": document_id,
                "first_seen_at": _now_iso(),
            }
            phys_key = _safe_fingerprint(document_path)
            if phys_key:
                payload["physical_cache_key"] = phys_key
            meta_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
    for cid, data in new_chunks.items():
        path = chunks_dir / f"{cid}.json"
        try:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            msg = f"warn: не удалось сохранить chunk {cid} в document-cache"
            if progress is not None:
                progress(msg)
            else:
                from sys import stderr
                print(f"[legal_summarizer] {msg}", file=stderr)


__all__ = [
    "doc_cache_dir",
    "load_doc_cache",
    "load_doc_cache_meta",
    "cache_is_fresh",
    "save_doc_cache",
]

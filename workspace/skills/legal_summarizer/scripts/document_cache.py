"""Document cache: per-session chunk-results для follow-up вопросов.

Контракт:
    * Cache живёт в ``workspace/data_store/cache/sessions/<session_key>/
      skills/legal_summarizer/documents/<document_id>/chunks/``.
    * Включается только если путь к файлу содержит session-папку
      (``SessionFileRedirectHook`` convention).
    * Используется для пропуска map-фазы при повторных вопросах к тому
      же документу в той же сессии (экономия 3-5 минут).

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


def load_doc_cache(
    document_id: str,
    session_key: str,
    workspace_root: Path,
) -> dict[str, dict]:
    """Загрузить chunks из document-cache. ``{chunk_id: chunk_data}``."""
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


def save_doc_cache(
    document_id: str,
    session_key: str,
    workspace_root: Path,
    new_chunks: dict[str, dict],
    *,
    progress: Any = None,
) -> None:
    """Сохранить новые chunks в document-cache (атомарно по файлу).

    Args:
        progress: callable(str) для вывода ошибки сохранения. Если
            ``None`` — fallback на ``print(..., file=sys.stderr)``.
            Сохранёнено для совместимости с ``summarizer._progress``.
    """
    if not new_chunks:
        return
    cache = doc_cache_dir(document_id, session_key, workspace_root)
    chunks_dir = cache / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    meta_path = cache / "meta.json"
    if not meta_path.exists():
        try:
            meta_path.write_text(
                json.dumps(
                    {
                        "document_id": document_id,
                        "first_seen_at": _now_iso(),
                    },
                    ensure_ascii=False,
                ),
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


__all__ = ["doc_cache_dir", "load_doc_cache", "save_doc_cache"]

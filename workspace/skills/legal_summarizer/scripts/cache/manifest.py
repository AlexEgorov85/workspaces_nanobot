"""Manifest v2 для legal_summarizer Phase 2B.

Manifest хранит source of truth для resume (invariant #12, #13, #14):
  * ``chunk_states`` — per-chunk state (status, section_id, page range, ...)
  * ``context_batches`` — list of batches
  * ``sections`` — sections tree (derived, пересчитывается при необходимости)
  * ``section_summaries`` — per-section summary (для hierarchical reduce)

Поддерживается только формат v2. ``load_manifest`` возвращает ``None``
для несовместимых манифестов (legacy v1 normalizer удалён).

Ответственность: **operation-level** resume state
(``operations/<operation_id>/manifest.json``, ``operations/<op_id>/chunks/*.json``,
``operations/<op_id>/result.json``).

Document-level cache (cross-operation identity, ``sessions/<key>/documents/<doc_id>/...``)
живёт в ``cache.document_cache.DocumentCache``. Этот модуль **не** владеет
document-level storage protocol и **не** экспортирует соответствующие функции.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MANIFEST_VERSION_V2 = 2


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, default=str, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


@dataclass
class NormalizedManifest:
    """Унифицированное представление manifest'а в формате v2."""

    operation_id: str
    status: str
    version: int
    document_path: str | None
    structure_title: str | None
    chars_in: int
    length: str
    chunks_total: int
    context_batches_total: int
    estimated_llm_calls: int | None
    actual_llm_calls: int | None
    sections: dict[str, dict[str, Any]]
    chunk_states: dict[str, dict[str, Any]]
    context_batches: dict[str, dict[str, Any]]
    section_summaries: dict[str, str]
    batches_done: list[str]
    batches_failed: list[str]
    last_error: dict[str, Any] | None
    started_at: str | None
    completed_at: str | None
    duration_sec: float | None
    article_count: int | None
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "operation_id": self.operation_id,
            "status": self.status,
            "document_path": self.document_path,
            "structure_title": self.structure_title,
            "chars_in": self.chars_in,
            "length": self.length,
            "chunks_total": self.chunks_total,
            "context_batches_total": self.context_batches_total,
            "estimated_llm_calls": self.estimated_llm_calls,
            "actual_llm_calls": self.actual_llm_calls,
            "sections": self.sections,
            "chunk_states": self.chunk_states,
            "context_batches": self.context_batches,
            "section_summaries": self.section_summaries,
            "batches_done": self.batches_done,
            "batches_failed": self.batches_failed,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_sec": self.duration_sec,
            "article_count": self.article_count,
            "raw": dict(self.raw) if self.raw else {},
        }


def skill_repo_root() -> Path:
    """Корень репозитория, выведенный из расположения этого скрипта.

    Модуль лежит по пути ``<repo>/workspace/skills/legal_summarizer/legal_summarizer/cache/manifest.py``.
    ``parents[5]`` от его абсолютного пути — корень репо.
    """
    return Path(__file__).resolve().parents[5]


def manifest_root(workspace_root: Path | str | None) -> Path:
    """Корень для manifest'ов/chunks/result skill'а (operation-level).

    ``workspace_root`` — корень РЕПО (не workspace dir!). Если не передан
    — выводится через :func:`skill_repo_root` (стабильный абсолютный путь).
    Возвращает ``<repo>/workspace/data_store/cache/skills/legal_summarizer``.
    """
    if workspace_root is None:
        workspace_root = skill_repo_root()
    return Path(workspace_root) / "workspace" / "data_store" / "cache" / "skills" / "legal_summarizer"


def manifest_path(operation_id: str, workspace_root: Path | str | None = None) -> Path:
    return manifest_root(workspace_root) / operation_id / "manifest.json"


def chunks_dir(operation_id: str, workspace_root: Path | str | None = None) -> Path:
    return manifest_root(workspace_root) / operation_id / "chunks"


def chunk_result_path(
    operation_id: str,
    chunk_id: str,
    workspace_root: Path | str | None = None,
) -> Path:
    return chunks_dir(operation_id, workspace_root) / f"{chunk_id}.json"


def result_path(operation_id: str, workspace_root: Path | str | None = None) -> Path:
    return manifest_root(workspace_root) / operation_id / "result.json"


def _detect_version(raw: dict[str, Any]) -> int | None:
    """Вернуть ``MANIFEST_VERSION_V2`` только для v2 manifest, иначе ``None``.

    Legacy v1 manifest не поддерживается (normalizer удалён).
    """
    if "version" in raw:
        try:
            return MANIFEST_VERSION_V2 if int(raw["version"]) == MANIFEST_VERSION_V2 else None
        except (TypeError, ValueError):
            return None
    if "chunk_states" in raw or "context_batches" in raw:
        return MANIFEST_VERSION_V2
    return None


def _normalize_v2(raw: dict[str, Any]) -> NormalizedManifest:
    return NormalizedManifest(
        operation_id=str(raw.get("operation_id", "")),
        status=str(raw.get("status", "running")),
        version=MANIFEST_VERSION_V2,
        document_path=raw.get("document_path"),
        structure_title=raw.get("structure_title"),
        chars_in=int(raw.get("chars_in") or 0),
        length=str(raw.get("length", "medium")),
        chunks_total=int(raw.get("chunks_total") or 0),
        context_batches_total=int(raw.get("context_batches_total") or 0),
        estimated_llm_calls=raw.get("estimated_llm_calls"),
        actual_llm_calls=raw.get("actual_llm_calls"),
        sections=dict(raw.get("sections") or {}),
        chunk_states=dict(raw.get("chunk_states") or {}),
        context_batches=dict(raw.get("context_batches") or {}),
        section_summaries=dict(raw.get("section_summaries") or {}),
        batches_done=list(raw.get("batches_done") or []),
        batches_failed=list(raw.get("batches_failed") or []),
        last_error=raw.get("last_error"),
        started_at=raw.get("started_at"),
        completed_at=raw.get("completed_at"),
        duration_sec=raw.get("duration_sec"),
        article_count=raw.get("article_count"),
        raw=dict(raw.get("raw") or {}),
    )


def load_manifest(
    operation_id: str,
    workspace_root: Path | str | None = None,
) -> NormalizedManifest | None:
    """Прочитать manifest.json и нормализовать к формату v2 in-memory."""
    raw = _read_json(manifest_path(operation_id, workspace_root))
    if raw is None:
        return None
    version = _detect_version(raw)
    if version != MANIFEST_VERSION_V2:
        return None
    return _normalize_v2(raw)


def save_manifest(
    normalized: NormalizedManifest,
    workspace_root: Path | str | None = None,
) -> None:
    """Записать manifest в формате v2 на диск."""
    payload = normalized.to_dict()
    payload["version"] = MANIFEST_VERSION_V2
    _atomic_write_json(manifest_path(normalized.operation_id, workspace_root), payload)


def write_chunk_result(
    operation_id: str,
    chunk_id: str,
    summary: str,
    *,
    context_batch_id: str | None,
    section_id: str | None,
    section_path: str | None,
    page_start: int | None,
    page_end: int | None,
    duration_sec: float | None,
    workspace_root: Path | str | None = None,
) -> None:
    """Сохранить per-chunk partial на диск (operation-level)."""
    payload = {
        "chunk_id": chunk_id,
        "summary": summary,
        "context_batch_id": context_batch_id,
        "section_id": section_id,
        "section_path": section_path,
        "page_start": page_start,
        "page_end": page_end,
        "duration_sec": duration_sec,
    }
    _atomic_write_json(chunk_result_path(operation_id, chunk_id, workspace_root), payload)


def read_chunk_result(
    operation_id: str,
    chunk_id: str,
    workspace_root: Path | str | None = None,
) -> dict[str, Any] | None:
    """Прочитать per-chunk partial. None если файла нет."""
    return _read_json(chunk_result_path(operation_id, chunk_id, workspace_root))


def write_result(
    operation_id: str,
    result: dict[str, Any],
    workspace_root: Path | str | None = None,
) -> None:
    """Сохранить финальный result.json."""
    _atomic_write_json(result_path(operation_id, workspace_root), result)


def read_result(
    operation_id: str,
    workspace_root: Path | str | None = None,
) -> dict[str, Any] | None:
    return _read_json(result_path(operation_id, workspace_root))


def load_cached_partials(
    operation_id: str,
    expected_chunk_ids: list[str],
    workspace_root: Path | str | None,
) -> dict[str, str]:
    """Загрузить per-chunk summary из disk-манифеста (operation/chunks/*.json)."""
    out: dict[str, str] = {}
    for cid in expected_chunk_ids:
        rec = read_chunk_result(operation_id, cid, workspace_root)
        if rec and isinstance(rec.get("summary"), str):
            out[cid] = rec["summary"]
    return out


__all__ = [
    "MANIFEST_VERSION_V2",
    "NormalizedManifest",
    "load_manifest",
    "save_manifest",
    "write_chunk_result",
    "read_chunk_result",
    "write_result",
    "read_result",
    "manifest_path",
    "manifest_root",
    "chunks_dir",
    "chunk_result_path",
    "result_path",
    "load_cached_partials",
    "skill_repo_root",
]

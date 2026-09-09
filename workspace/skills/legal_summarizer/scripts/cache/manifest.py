"""Manifest v2 для legal_summarizer Phase 2B.

Manifest хранит source of truth для resume (invariant #12, #13, #14):
  * ``chunk_states`` — per-chunk state (status, section_id, page range, ...)
  * ``context_batches`` — list of batches
  * ``sections`` — sections tree (derived, пересчитывается при необходимости)
  * ``section_summaries`` — per-section summary (для hierarchical reduce)

Поддерживается только формат v2. ``load_manifest`` возвращает ``None``
для несовместимых манифестов (legacy v1 normalizer удалён).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workspace.utils.session_key import safe_session_key


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
    ``parents[5]`` от его абсолютного пути — корень репо. Это СТАБИЛЬНЫЙ
    якорь, не зависящий от cwd процесса и от того, как агент запустил cli.py
    (раньше при ``workspace_root=None`` возвращался относительный путь
    ``workspace/data_store/...``, и при cwd=<workspace> получался дубль
    ``workspace/workspace/data_store/...`` — см. тест-инцидент 2026-08-28).
    """
    return Path(__file__).resolve().parents[5]


def manifest_root(workspace_root: Path | str | None) -> Path:
    """Корень для manifest'ов/chunks/result skill'а.

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
    """Прочитать manifest.json и нормализовать к формату v2 in-memory.

    Возвращает ``None`` если файла нет или это не v2 manifest
    (legacy v1 normalizer удалён — несовместимые манифесты игнорируются).
    """
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
    """Сохранить per-chunk partial на диск."""
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
    # Document-level cache (cross-operation identity).
    "document_dir",
    "document_physical_path",
    "document_analysis_path",
    "document_chunks_dir",
    "document_chunk_result_path",
    "document_section_result_path",
    "is_document_cache_complete",
    "write_document_section_summary",
    "write_document_chunk_summary",
    "read_document_chunk_summary",
    "load_document_chunk_summaries",
    "read_document_section_summary",
    "load_document_section_summaries",
    "write_document_snapshot",
    "read_document_snapshot",
    "invalidate_document_cache",
]


def load_cached_partials(
    operation_id: str,
    expected_chunk_ids: list[str],
    workspace_root: Path | str | None,
) -> dict[str, str]:
    """Загрузить per-chunk summary из disk-манифеста (operation/chunks/*.json).

    Canonical location: ``cache.manifest``. Раньше жил в
    ``execution.pipeline``, но cache lookup — это ответственность
    application layer (cache boundary), не execution.
    """
    out: dict[str, str] = {}
    for cid in expected_chunk_ids:
        rec = read_chunk_result(operation_id, cid, workspace_root)
        if rec and isinstance(rec.get("summary"), str):
            out[cid] = rec["summary"]
    return out


# ---------------------------------------------------------------------------
# Document-level cache (cross-operation identity)
# ---------------------------------------------------------------------------
#
# ``DocumentIdentity`` строится из (resolved_path, size, mtime_ns) → SHA-256
# (см. ``document.identity.DocumentIdentity.from_path``). Это даёт стабильный
# ключ для повторных запусков над тем же файлом **без** зависимости от
# question/length/text hash (которые меняют ``operation_id``).
#
# Layout на диске (под ``document_dir(document_id, workspace_root, session_key)``):
#
#     _complete.marker                       # существует только при успешной записи
#     physical.json                          # PhysicalDocument.to_dict()
#     analysis.json                          # {identity, structure, chunks, validation}
#     chunks/<chunk_id>.json                 # {summary, section_id, section_path, page_start, page_end}
#     sections/<section_id>.json             # section-level LLM summary
#
# Корень: ``<repo>/workspace/data_store/cache/sessions/<safe_session_key>/documents/``.
# Привязка к сессии: внутри одной сессии тот же ``document_id`` (SHA-256 от
# resolved_path+size+mtime_ns) → cache hit. Между сессиями переиспользования
# нет, каждая сессия живёт в своей подпапке.
#
# Snapshot пишется атомарно: staging dir + Path.rename. ``_complete.marker``
# создаётся последним. Без marker snapshot считается неполным (cache miss).


def _document_cache_root(
    workspace_root: Path | str | None,
    session_key: str,
) -> Path:
    """Корень document-level cache для конкретной сессии.

    ``<repo>/workspace/data_store/cache/sessions/<safe_session_key>/documents/``.

    Вынесен из ``document_dir``, чтобы не зависеть от ``manifest_root``
    (тот по-прежнему обслуживает operation-level namespace
    ``skills/legal_summarizer/operations/<op_id>/``).
    """
    root = Path(workspace_root) if workspace_root is not None else skill_repo_root()
    safe = safe_session_key(session_key or "default")
    return (
        root
        / "workspace" / "data_store" / "cache"
        / "sessions" / safe / "documents"
    )


def document_dir(
    document_id: str,
    workspace_root: Path | str | None = None,
    session_key: str = "default",
) -> Path:
    """Корневая папка document-level cache.

    ``<repo>/workspace/data_store/cache/sessions/<safe_session_key>/documents/<document_id>``.

    Layout привязан к сессии (Phase 7 Resource Model Refactoring): тот же
    файл, загруженный повторно в той же сессии → cache hit. Между сессиями
    переиспользования нет: каждая сессия живёт в своей подпапке.
    """
    return _document_cache_root(workspace_root, session_key) / document_id


def document_physical_path(
    document_id: str,
    workspace_root: Path | str | None = None,
    session_key: str = "default",
) -> Path:
    return document_dir(document_id, workspace_root, session_key) / "physical.json"


def document_analysis_path(
    document_id: str,
    workspace_root: Path | str | None = None,
    session_key: str = "default",
) -> Path:
    return document_dir(document_id, workspace_root, session_key) / "analysis.json"


def document_chunks_dir(
    document_id: str,
    workspace_root: Path | str | None = None,
    session_key: str = "default",
) -> Path:
    return document_dir(document_id, workspace_root, session_key) / "chunks"


def document_chunk_result_path(
    document_id: str,
    chunk_id: str,
    workspace_root: Path | str | None = None,
    session_key: str = "default",
) -> Path:
    return document_chunks_dir(document_id, workspace_root, session_key) / f"{chunk_id}.json"


def _document_complete_marker_path(
    document_id: str,
    workspace_root: Path | str | None = None,
    session_key: str = "default",
) -> Path:
    return document_dir(document_id, workspace_root, session_key) / "_complete.marker"


def document_section_result_path(
    document_id: str,
    section_id: str,
    workspace_root: Path | str | None = None,
    session_key: str = "default",
) -> Path:
    return document_dir(document_id, workspace_root, session_key) / "sections" / f"{section_id}.json"


def is_document_cache_complete(
    document_id: str,
    workspace_root: Path | str | None = None,
    session_key: str = "default",
) -> bool:
    """Быстрая проверка наличия snapshot'а (stat по marker'у)."""
    return _document_complete_marker_path(
        document_id, workspace_root, session_key,
    ).is_file()


def write_document_section_summary(
    *,
    workspace_root: Path | str | None,
    document_id: str,
    section_id: str,
    summary: str,
    question: str | None = None,
    session_key: str = "default",
) -> None:
    """Записать per-section LLM summary в document-level cache.

    document-level cache хранит ТОЛЬКО question-independent
    (baseline) summaries. Если передан ``question is not None`` —
    summary был построен с учётом конкретного вопроса и НЕ должен
    попасть в cross-operation cache. Operation-level cache
    (``operations/<op_id>/manifest.json:section_summaries``) хранит
    question-specific результаты.

    Append-only и атомарный. Используется после успешного map/reduce —
    отдельная стадия жизненного цикла, не часть ``write_document_snapshot``.
    """
    if question is not None:
        return
    if not document_id or not section_id or not summary:
        return
    payload = {"section_id": section_id, "summary": summary}
    _atomic_write_json(
        document_section_result_path(
            document_id, section_id, workspace_root, session_key,
        ),
        payload,
    )


def write_document_chunk_summary(
    *,
    workspace_root: Path | str | None,
    document_id: str,
    chunk_id: str,
    summary: str,
    section_id: str | None = None,
    section_path: str | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
    question: str | None = None,
    session_key: str = "default",
) -> None:
    """Записать per-chunk LLM summary в document-level cache.

    document-level cache хранит ТОЛЬКО question-independent
    (baseline) summaries. Если передан ``question is not None`` —
    summary был построен с учётом конкретного вопроса и НЕ должен
    попасть в cross-operation cache (semantic pollution guard).
    В этом случае функция возвращает no-op.

    Append-only и атомарный. Используется после успешного batch'а map-фазы —
    параллельно с записью в ``operations/<op_id>/chunks/<cid>.json``.
    """
    if question is not None:
        # Question-specific summary → operation-level cache only.
        return
    if not document_id or not chunk_id or not summary:
        return
    payload = {
        "chunk_id": chunk_id,
        "summary": summary,
        "section_id": section_id,
        "section_path": section_path,
        "page_start": page_start,
        "page_end": page_end,
    }
    _atomic_write_json(
        document_chunk_result_path(
            document_id, chunk_id, workspace_root, session_key,
        ),
        payload,
    )


def read_document_chunk_summary(
    document_id: str,
    chunk_id: str,
    workspace_root: Path | str | None = None,
    session_key: str = "default",
) -> dict[str, Any] | None:
    """Прочитать per-chunk summary из document-level cache."""
    return _read_json(
        document_chunk_result_path(
            document_id, chunk_id, workspace_root, session_key,
        ),
    )


def load_document_chunk_summaries(
    document_id: str,
    expected_chunk_ids: list[str],
    workspace_root: Path | str | None = None,
    session_key: str = "default",
) -> dict[str, str]:
    """Загрузить per-chunk summaries из document-level cache.

    Cross-operation cache lookup — отличаётся от ``load_cached_partials``
    тем, что не привязан к конкретному ``operation_id``. Используется
    для question synthesis поверх document-level cache.
    """
    out: dict[str, str] = {}
    for cid in expected_chunk_ids:
        rec = read_document_chunk_summary(
            document_id, cid, workspace_root, session_key,
        )
        if rec and isinstance(rec.get("summary"), str):
            out[cid] = rec["summary"]
    return out


def read_document_section_summary(
    document_id: str,
    section_id: str,
    workspace_root: Path | str | None = None,
    session_key: str = "default",
) -> dict[str, Any] | None:
    """Прочитать per-section summary из document-level cache."""
    return _read_json(
        document_section_result_path(
            document_id, section_id, workspace_root, session_key,
        ),
    )


def load_document_section_summaries(
    document_id: str,
    expected_section_ids: list[str],
    workspace_root: Path | str | None = None,
    session_key: str = "default",
) -> dict[str, str]:
    """Загрузить per-section summaries для списка section_id."""
    out: dict[str, str] = {}
    for sid in expected_section_ids:
        rec = read_document_section_summary(
            document_id, sid, workspace_root, session_key,
        )
        if rec and isinstance(rec.get("summary"), str):
            out[sid] = rec["summary"]
    return out


def write_document_snapshot(
    *,
    workspace_root: Path | str | None,
    document_id: str,
    physical_data: dict[str, Any],
    analysis_data: dict[str, Any],
    retrieval_index_meta: dict[str, Any] | None = None,
    session_key: str = "default",
) -> Path:
    """Атомарная запись document-level snapshot'а.

    Используется на cache miss после успешного ``run_canonical_pipeline``.
    Плишет все файлы + ``_complete.marker`` через временный staging
    каталог + ``Path.rename`` — на случай падения посередине целостный
    snapshot либо виден полностью, либо не существует.

    Args:
        workspace_root: корень репо.
        document_id: ``DocumentIdentity.document_id``.
        physical_data: ``PhysicalDocument.to_dict()``.
        analysis_data: ``DocumentAnalysis.to_dict()`` минус
            ``physical_path``, ``has_retrieval_index`` (физический
            хранится отдельно в ``physical.json``, retrieval_index
            восстанавливается из L1/L2).
        retrieval_index_meta: метаданные для ``retrieval_index.meta.json``
            (``{"chunk_count": N, "term_count": M}``); None → skip.
        session_key: ключ сессии для session-scoped пути.

    Returns:
        Путь к финальному ``document_dir(document_id, session_key)``.

    Raises:
        ``RuntimeError`` если snapshot уже существует (нельзя
        перезаписывать без явного ``overwrite=True``; защита от
        случайной перезаписи согласованного snapshot'а).
    """
    if not document_id:
        raise ValueError("write_document_snapshot: document_id обязателен")

    target_dir = document_dir(document_id, workspace_root, session_key)
    if target_dir.exists() and is_document_cache_complete(
        document_id, workspace_root, session_key,
    ):
        raise RuntimeError(
            f"document-level cache для document_id={document_id!r} уже complete; "
            "перезапись запрещена. Используйте explicit invalidate перед "
            "повторной записью (см. также DocumentIdentity.is_fresh)."
        )

    # Staging dir + atomic rename.
    import shutil
    import tempfile

    # staging_parent — это сессионная подпапка, чтобы tmp-каталог гарантированно
    # лежал рядом с финальным snapshot'ом (требование os.rename для атомарности).
    staging_parent = _document_cache_root(workspace_root, session_key)
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".staging_doc_{document_id}_", dir=staging_parent))

    try:
        # 1. physical.json
        _atomic_write_json(staging / "physical.json", physical_data)

        # 2. analysis.json
        _atomic_write_json(staging / "analysis.json", analysis_data)

        # 3. retrieval_index.meta.json (опционально)
        if retrieval_index_meta is not None:
            (staging / "retrieval_index.meta.json").write_text(
                json.dumps(retrieval_index_meta, ensure_ascii=False, default=str),
                encoding="utf-8",
            )

        # 4. marker — создаётся последним, гарантирует атомарность.
        (staging / "_complete.marker").write_text(
            json.dumps(
                {
                    "version": 1,
                    "completed_at": __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc,
                    ).isoformat(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # 5. Atomic rename: если target уже был — перезаписываем.
        # Windows-специфика: Path.rename → os.rename, требует чтобы
        # target.parent существовал. POSIX: parent создаётся автоматически.
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        if target_dir.exists():
            shutil.rmtree(target_dir)
        staging.rename(target_dir)
        return target_dir
    except Exception:
        # Cleanup staging при любой ошибке.
        if staging.exists() and staging.is_dir():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def read_document_snapshot(
    document_id: str,
    workspace_root: Path | str | None = None,
    session_key: str = "default",
) -> tuple[
    dict[str, Any] | None,    # physical
    dict[str, Any] | None,    # analysis
    dict[str, Any] | None,    # retrieval_index.meta
] | None:
    """Прочитать document-level snapshot.

    Возвращает ``None`` если snapshot неполный (нет ``_complete.marker``)
    или ``document_dir`` не существует.

    Returns:
        ``(physical_data, analysis_data, retrieval_meta)`` — каждый
        элемент это ``dict`` от ``_read_json`` или ``None`` если
        соответствующий файл не читается / отсутствует.
    """
    if not is_document_cache_complete(document_id, workspace_root, session_key):
        return None

    physical = _read_json(
        document_physical_path(document_id, workspace_root, session_key),
    )
    analysis = _read_json(
        document_analysis_path(document_id, workspace_root, session_key),
    )
    meta = _read_json(
        document_dir(document_id, workspace_root, session_key)
        / "retrieval_index.meta.json",
    )
    return physical, analysis, meta


def invalidate_document_cache(
    document_id: str,
    workspace_root: Path | str | None = None,
    session_key: str = "default",
) -> None:
    """Удалить document-level snapshot целиком.

    Используется при ``DocumentIdentity.is_fresh() == False`` (mtime
    изменился) или при явном сбросе. Безопасно вызывать на несуществующем
    каталоге (no-op).
    """
    import shutil

    target = document_dir(document_id, workspace_root, session_key)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

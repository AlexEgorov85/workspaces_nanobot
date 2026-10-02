"""DocumentCache — единственный владелец document-level cache protocol.

Ответственность (жёсткая граница, см. ``docs/TARGET_ARCHITECTURE.md``):

* хранение snapshot'а (``physical.json``, ``analysis.json``, ``retrieval_index.meta.json``);
* marker completeness (``_complete.marker``);
* per-chunk / per-section summaries;
* atomic write через staging dir + ``Path.rename``;
* invalidation snapshot'а целиком;
* session-scoped path layout;
* JSON serialization.

Не ответственность (вызывающий код должен делать это сам):

* chunk selection / ranking — ``application.chunk_selection``;
* question synthesis — ``application.question_context``;
* pipeline orchestration — ``application.pipeline_structure``;
* LLM calls / business logic — ``execution.*``, ``llm.*``;
* operation-level resume state — ``cache.manifest``.

Layout на диске (под ``document_dir(document_id)``)::

    _complete.marker                       # marker completeness
    physical.json                          # PhysicalDocument.to_dict()
    analysis.json                          # {identity, structure, chunks, validation}
    retrieval_index.meta.json              # {chunk_count, term_count} — optional
    chunks/<chunk_id>.json                 # per-chunk summary
    sections/<section_id>.json             # per-section LLM summary

Корень: ``<repo>/workspace/data_store/cache/sessions/<safe_session_key>/documents/``.
Привязка к сессии: в одной сессии тот же ``document_id`` (SHA-256 от
resolved_path+size+mtime_ns) → cache hit. Между сессиями переиспользования нет.

Snapshot пишется атомарно: staging dir + ``Path.rename``. ``_complete.marker``
создаётся последним. Без marker snapshot считается неполным (cache miss).

API: instance ``DocumentCache(workspace_root, session_key)``. ``workspace_root``
и ``session_key`` — это конфигурация cache, общая для всех операций в
рамках одного запроса. Caller создаёт ``DocumentCache`` один раз и
передаёт в методы только ``document_id`` (или ``chunk_id``/``section_id``),
которые являются частью самой операции.

Это **не** wrapper над старым API — единственный источник истины. Любой
production-модуль (``application/``, ``execution/``, ``retrieval/``,
``output/``, ``planning/``, ``chunking/``, ``document/``, ``llm/``) **не должен**
импортировать ничего из ``cache.manifest`` document-level API. Это контракт
``tests/architecture/test_document_cache_boundaries.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workspace.utils.session_key import safe_session_key


__all__ = ["DocumentCache"]


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Атомарная запись JSON: tmp + replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, default=str, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    """Прочитать JSON; вернуть ``None`` если файла нет или он битый."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _skill_repo_root() -> Path:
    """Стабильный абсолютный корень репо, выведенный из расположения этого файла.

    ``<repo>/workspace/skills/legal_summarizer/scripts/cache/document_cache.py``.
    ``parents[5]`` → корень репо. Не зависит от cwd процесса.
    """
    return Path(__file__).resolve().parents[5]


def _cache_root(workspace_root: Path | str | None, session_key: str) -> Path:
    """Корень document-level cache для конкретной сессии.

    ``<repo>/workspace/data_store/cache/sessions/<safe_session_key>/documents/``.
    """
    root = Path(workspace_root) if workspace_root is not None else _skill_repo_root()
    safe = safe_session_key(session_key or "default")
    return (
        root
        / "workspace" / "data_store" / "cache"
        / "sessions" / safe / "documents"
    )


class DocumentCache:
    """Canonical API document-level cache.

    Instance инкапсулирует общий cache configuration
    (``workspace_root``, ``session_key``) — caller создаёт ``DocumentCache``
    один раз в начале запроса и переиспользует для всех операций.
    Методы получают только то, что является частью конкретной операции
    (``document_id``, ``chunk_id``, ``section_id``).
    """

    __slots__ = ("workspace_root", "session_key")

    def __init__(
        self,
        workspace_root: Path | str | None,
        session_key: str = "default",
    ) -> None:
        self.workspace_root = workspace_root
        self.session_key = session_key or "default"

    # ──────────────────────────────────────────────────────────────────────
    # Paths (private; production не должен знать cache layout).
    # ──────────────────────────────────────────────────────────────────────

    def _document_dir(self, document_id: str) -> Path:
        """Корневая папка document cache. **Private** — не для production."""
        return _cache_root(self.workspace_root, self.session_key) / document_id

    def _marker_path(self, document_id: str) -> Path:
        """Путь к ``_complete.marker``. **Private** — не для production."""
        return self._document_dir(document_id) / "_complete.marker"

    # ──────────────────────────────────────────────────────────────────────
    # Completeness.
    # ──────────────────────────────────────────────────────────────────────

    def is_complete(self, document_id: str) -> bool:
        """``True`` если snapshot существует и marker на месте.

        Дешёвая проверка (stat по marker'у). Не читает payload — это
        прерогатива :meth:`read_snapshot`.
        """
        return self._marker_path(document_id).is_file()

    # ──────────────────────────────────────────────────────────────────────
    # Snapshot read/write/invalidate.
    # ──────────────────────────────────────────────────────────────────────

    def write_snapshot(
        self,
        *,
        document_id: str,
        physical_data: dict[str, Any],
        analysis_data: dict[str, Any],
        retrieval_index_meta: dict[str, Any] | None = None,
    ) -> Path:
        """Атомарная запись document-level snapshot'а.

        Concurrency contract:

        * если ``target_dir`` уже **complete** (marker на месте) —
          поднимается ``RuntimeError`` (поверх существующего snapshot
          не писать; нужно сначала :meth:`invalidate`);
        * атомарность ``install/replace`` обеспечивается через
          :func:`os.replace` — POSIX и Windows оба делают rename
          атомарно, без ``rmtree + rename`` (которое создаёт TOCTOU
          window, в котором параллельный writer может уничтожить
          чужой complete snapshot).

        Snapshot пишется в staging dir + ``os.replace``. ``_complete.marker``
        создаётся последним — на случай падения посередине целостный
        snapshot либо виден полностью, либо не существует.

        Args:
            document_id: ``DocumentIdentity.document_id``.
            physical_data: ``PhysicalDocument.to_dict()``.
            analysis_data: ``DocumentAnalysis.to_dict()`` минус ``physical_path``
                и ``has_retrieval_index`` (физический хранится в ``physical.json``,
                retrieval_index восстанавливается из L1/L2).
            retrieval_index_meta: ``{"chunk_count": N, "term_count": M}`` или
                ``None``.

        Returns:
            Путь к финальному ``document_dir``.

        Raises:
            ValueError: ``document_id`` пустой.
            RuntimeError: snapshot уже complete — перезапись запрещена без
                явного :meth:`invalidate`.
        """
        if not document_id:
            raise ValueError("DocumentCache.write_snapshot: document_id обязателен")

        target_dir = self._document_dir(document_id)

        # Fail-fast check до того, как мы тратим работу на staging.
        # Между этим check и ``os.replace`` параллельный writer мог
        # завершить свой snapshot — это нормально, ``os.replace``
        # атомарно перезапишет target. Но если target **уже complete**
        # к моменту check — мы не должны писать, нужно explicit
        # invalidate (так contract: complete → protected).
        if target_dir.exists() and self.is_complete(document_id):
            raise RuntimeError(
                f"document-level cache для document_id={document_id!r} уже complete; "
                "перезапись запрещена. Используйте DocumentCache.invalidate перед "
                "повторной записью."
            )

        staging_parent = _cache_root(self.workspace_root, self.session_key)
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".staging_doc_{document_id}_", dir=staging_parent))

        try:
            _atomic_write_json(staging / "physical.json", physical_data)
            _atomic_write_json(staging / "analysis.json", analysis_data)

            if retrieval_index_meta is not None:
                (staging / "retrieval_index.meta.json").write_text(
                    json.dumps(retrieval_index_meta, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )

            (staging / "_complete.marker").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            target_dir.parent.mkdir(parents=True, exist_ok=True)

            # Атомарный install/replace через ``os.replace`` (POSIX + Windows).
            # Устраняет TOCTOU window, в котором параллельный writer мог
            # уничтожить чужой complete snapshot через rmtree + rename.
            # Параллельные writer'ы пишут семантически эквивалентные
            # snapshot'ы (один document_id = один fingerprint), поэтому
            # «выигрыш» любого writer'а допустим; data loss невозможен.
            os.replace(staging, target_dir)

            # Post-write cleanup: удалить orphan siblings (старые версии
            # того же файла с другим document_id). Scan documents_root
            # по physical.json.path, сравниваем через os.path.realpath
            # для устойчивости к разному spelling (relative vs absolute,
            # POSIX vs Windows разделители). Идемпотентно: ``rmtree`` с
            # ignore_errors=True не падает на несуществующих siblings
            # (другой writer мог уже удалить их параллельно).
            self._evict_orphan_siblings(target_dir, physical_data)
            return target_dir
        except Exception:
            if staging.exists() and staging.is_dir():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    def _evict_orphan_siblings(
        self,
        target_dir: Path,
        physical_data: dict[str, Any],
    ) -> None:
        """Удалить snapshot'ы, соответствующие старым версиям того же файла.

        Orphan sibling = каталог в ``documents_root`` с другим
        ``document_id``, но тем же ``physical.path``. Возникает при
        изменении исходного файла (mtime/size → новый SHA-256 →
        новый document_id → новый snapshot; старый остаётся мёртвым
        грузом).

        Без этой cleanup document-level cache растёт неограниченно
        с каждой редакцией файла. Caller (pipeline) передаёт
        ``physical_data`` явно — он знает абсолютный путь. Сравнение
        через ``os.path.realpath`` нормализует разный spelling путей
        на разных OS.

        Идемпотентно: ``shutil.rmtree(..., ignore_errors=True)`` не
        падает на несуществующих siblings (другой writer мог удалить
        их параллельно).
        """
        source_path_raw = physical_data.get("path")
        if not source_path_raw:
            return
        source_real = os.path.realpath(source_path_raw)

        documents_root = target_dir.parent
        target_name = target_dir.name
        if not documents_root.is_dir():
            return

        for sibling in documents_root.iterdir():
            if not sibling.is_dir():
                continue
            if sibling.name == target_name:
                continue
            if sibling.name.startswith(".staging_"):
                continue
            sibling_physical = _read_json(sibling / "physical.json")
            if sibling_physical is None:
                continue
            sibling_path_raw = sibling_physical.get("path")
            if not sibling_path_raw:
                continue
            try:
                if os.path.realpath(sibling_path_raw) == source_real:
                    shutil.rmtree(sibling, ignore_errors=True)
            except OSError:
                # Path resolution failed (битый symlink и т.п.) — skip.
                continue

    def read_snapshot(
        self,
        document_id: str,
    ) -> tuple[
        dict[str, Any] | None,
        dict[str, Any] | None,
        dict[str, Any] | None,
    ] | None:
        """Прочитать document-level snapshot.

        Returns:
            ``(physical, analysis, retrieval_meta)`` или ``None`` если
            snapshot неполный (нет marker'а) или document_dir не существует.
            Каждый элемент = dict от ``_read_json`` или ``None`` если
            соответствующий файл не читается / отсутствует.
        """
        if not self.is_complete(document_id):
            return None

        doc_dir = self._document_dir(document_id)
        physical = _read_json(doc_dir / "physical.json")
        analysis = _read_json(doc_dir / "analysis.json")
        meta = _read_json(doc_dir / "retrieval_index.meta.json")
        return physical, analysis, meta

    def invalidate(self, document_id: str) -> None:
        """Удалить document-level snapshot целиком.

        Безопасно вызывать на несуществующем каталоге (no-op).
        Используется при битом snapshot (catch в ``from_dict``) или
        при явном сбросе. Change detection при изменении файла
        работает через ``document_id`` hash: новый файл → новый
        fingerprint → cache miss → reparse → старый snapshot остаётся
        как orphan. ``invalidate`` здесь — explicit cleanup, не part
        нормального cache lifecycle.
        """
        target = self._document_dir(document_id)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)

    # ──────────────────────────────────────────────────────────────────────
    # Chunk summaries.
    # ──────────────────────────────────────────────────────────────────────

    def _chunk_path(self, document_id: str, chunk_id: str) -> Path:
        return self._document_dir(document_id) / "chunks" / f"{chunk_id}.json"

    def write_chunk_summary(
        self,
        *,
        document_id: str,
        chunk_id: str,
        summary: str,
        section_id: str | None = None,
        section_path: str | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
        question: str | None = None,
    ) -> None:
        """Записать per-chunk LLM summary в document-level cache.

        document-level cache хранит ТОЛЬКО question-independent (baseline)
        summaries. Если ``question is not None`` — summary был построен с
        учётом конкретного вопроса и НЕ должен попасть в cross-operation
        cache (semantic pollution guard). В этом случае no-op.

        Идемпотентный atomic upsert: повторный write с тем же
        ``chunk_id`` перезаписывает существующий summary (не append-only).
        Атомарность обеспечивается через ``_atomic_write_json``.
        """
        if question is not None:
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
            self._chunk_path(document_id, chunk_id),
            payload,
        )

    def _read_chunk_summary(
        self,
        document_id: str,
        chunk_id: str,
    ) -> dict[str, Any] | None:
        return _read_json(self._chunk_path(document_id, chunk_id))

    def load_chunk_summaries(
        self,
        document_id: str,
        expected_chunk_ids: list[str],
    ) -> dict[str, str]:
        """Загрузить per-chunk summaries из document-level cache.

        Cross-operation lookup — не привязан к ``operation_id``. Используется
        для question synthesis поверх document-level cache.
        """
        out: dict[str, str] = {}
        for cid in expected_chunk_ids:
            rec = self._read_chunk_summary(document_id, cid)
            if rec and isinstance(rec.get("summary"), str):
                out[cid] = rec["summary"]
        return out

    # ──────────────────────────────────────────────────────────────────────
    # Section summaries.
    # ──────────────────────────────────────────────────────────────────────

    def _section_path(self, document_id: str, section_id: str) -> Path:
        return self._document_dir(document_id) / "sections" / f"{section_id}.json"

    def write_section_summary(
        self,
        *,
        document_id: str,
        section_id: str,
        summary: str,
        question: str | None = None,
    ) -> None:
        """Записать per-section LLM summary в document-level cache.

        document-level cache хранит ТОЛЬКО question-independent (baseline)
        summaries. ``question is not None`` → no-op.

        Идемпотентный atomic upsert: повторный write с тем же
        ``section_id`` перезаписывает существующий summary.
        Используется после успешного map/reduce — отдельная стадия
        жизненного цикла.
        """
        if question is not None:
            return
        if not document_id or not section_id or not summary:
            return
        payload = {"section_id": section_id, "summary": summary}
        _atomic_write_json(
            self._section_path(document_id, section_id),
            payload,
        )

    def _read_section_summary(
        self,
        document_id: str,
        section_id: str,
    ) -> dict[str, Any] | None:
        return _read_json(self._section_path(document_id, section_id))

    def load_section_summaries(
        self,
        document_id: str,
        expected_section_ids: list[str],
    ) -> dict[str, str]:
        """Загрузить per-section summaries из document-level cache."""
        out: dict[str, str] = {}
        for sid in expected_section_ids:
            rec = self._read_section_summary(document_id, sid)
            if rec and isinstance(rec.get("summary"), str):
                out[sid] = rec["summary"]
        return out

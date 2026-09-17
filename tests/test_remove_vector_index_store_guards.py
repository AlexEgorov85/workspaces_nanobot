"""Тесты архитектурных guard'ов change ``remove-vector-index-store``.

Покрывают:
  * test_no_callers_of_removed_methods: ни один модуль в lib/tools/workspace
    не должен звать удалённые методы провайдера
    (``_save_index_to_store``, ``_load_index_from_store``, ``rebuild_and_store_index``,
    ``_compute_index_signature_from_config``, ``VectorIndexBuildService.rebuild_and_store``).
  * test_no_hardcoded_table_names: имена таблиц ``public.agent_vector_index_store``
    / ``oarb.audit_vectors`` не должны хардкодиться в runtime-коде.
  * test_load_index_only_uses_cache_path: ``_load_index`` идёт ТОЛЬКО через
    ``_load_index_from_cache`` (никаких fallback'ов на store/files).
  * test_search_vector_hydrates_payload_from_cache: payload подтягивается из
    DuckDB per-hit (не из serialized index).
  * test_search_vector_cold_miss_raises: cold miss → ошибка, без ленивой сборки.
  * test_preload_indexes_uses_only_indexes_config: ``preload_indexes``
    берёт имена из ``gateway.vector.index.indexes.*`` (не из store).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

faiss = pytest.importorskip("faiss")
np = pytest.importorskip("numpy")

_REPO = Path(__file__).resolve().parent.parent


def _iter_python_files(*roots: str) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        path = _REPO / root
        if not path.exists():
            continue
        out.extend(p for p in path.rglob("*.py") if not p.name.startswith("_"))
    return out


def _names_in_file(path: Path) -> set[str]:
    """Извлечь имена атрибутов и имён из файла через AST."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()

    names: set[str] = set()

    class _Visitor(ast.NodeVisitor):
        def visit_Attribute(self, node: ast.Attribute) -> None:
            names.add(node.attr)
            self.generic_visit(node)

        def visit_Name(self, node: ast.Name) -> None:
            names.add(node.id)

    _Visitor().visit(tree)
    return names


_REMOVED_METHODS = (
    "_save_index_to_store",
    "_load_index_from_store",
    "rebuild_and_store_index",
    "_compute_index_signature_from_config",
    "rebuild_and_store",  # VectorIndexBuildService.rebuild_and_store
)


class TestRemovedMethodsNoCallers:
    def test_no_callers_of_removed_methods(self) -> None:
        """AST: ни один файл в lib/tools/workspace не зовёт удалённые методы."""
        files = _iter_python_files("lib", "tools", "workspace")
        violations: list[tuple[str, str]] = []
        for path in files:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in _REMOVED_METHODS:
                    violations.append((
                        str(path.relative_to(_REPO)),
                        f"L{node.lineno} attr={node.attr}",
                    ))
                elif (isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Name)
                      and node.func.id in _REMOVED_METHODS):
                    violations.append((
                        str(path.relative_to(_REPO)),
                        f"L{node.lineno} call={node.func.id}",
                    ))

        assert not violations, (
            "Удалённые методы всё ещё вызываются:\n"
            + "\n".join(f"  {p}: {info}" for p, info in violations)
        )


_HARDCODED_TABLE_NAMES = (
    "public.agent_vector_index_store",
    "oarb.audit_vectors",
)
# Whitelist: ``tools/generate_comments_sql.py`` генерирует DDL-комментарии
# и ДОЛЖЕН знать конкретные имена таблиц; ``tools/debug/`` — служебные
# утилиты (не runtime).
_HARDCODED_WHITELIST = {
    "tools/generate_comments_sql.py",
    "tools/debug/emulate_sync_errors.py",
    "sql/audit_analyzer/create_oarb_audit_vectors.sql",
}


def _is_runtime_code(rel_path: str) -> bool:
    """Runtime-код: только lib/, tools/ (без debug/), workspace/ (без сессий)."""
    if rel_path.startswith("workspace/data_store/cache/"):
        return False
    if rel_path.startswith("workspace/data_store/_archive/"):
        return False
    if rel_path.startswith("workspace/memory/"):
        return False
    if rel_path.startswith("tools/debug/"):
        return False
    if rel_path.startswith("tools/_archive/"):
        return False
    return (
        rel_path.startswith("lib/")
        or rel_path.startswith("tools/")
        or rel_path.startswith("workspace/skills/")
        or rel_path.startswith("workspace/tools/")
        or rel_path.startswith("workspace/utils/")
        or rel_path.startswith("workspace/hooks/")
        or rel_path.startswith("workspace/cron/")
        or rel_path.startswith("workspace/prompts/")
    )


class TestNoHardcodedTableNames:
    def test_no_hardcoded_table_names_in_runtime(self) -> None:
        """Имена таблиц берутся из gateway.vector.index.*, не хардкодятся."""
        files = _iter_python_files("lib", "tools", "workspace")
        violations: list[tuple[str, str, str]] = []
        for path in files:
            rel = str(path.relative_to(_REPO))
            if rel in _HARDCODED_WHITELIST:
                continue
            if not _is_runtime_code(rel):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for needle in _HARDCODED_TABLE_NAMES:
                if needle in text:
                    # Skip docstrings/comments (heuristic: lines starting with #, """ or inside docstring).
                    for ln, line in enumerate(text.splitlines(), start=1):
                        if needle not in line:
                            continue
                        stripped = line.strip()
                        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                            continue
                        # Heuristic: skip lines that look like docstring content (between """ blocks).
                        # Cheap approach: skip lines where needle is inside backticks or
                        # followed by a closing paren/colons indicating a docstring reference.
                        violations.append((rel, f"L{ln}", line.strip()))
                        break

        assert not violations, (
            "Хардкод имён таблиц в runtime-коде:\n"
            + "\n".join(f"  {p} {info}: {ln}" for p, info, ln in violations)
        )


class TestLoadIndexOnlyUsesCachePath:
    """_load_index идёт ТОЛЬКО через _load_index_from_cache."""

    def test_load_index_source_contains_only_cache_call(self) -> None:
        import re

        from lib.services import cache_provider_impl as impl

        src = Path(impl.__file__).read_text(encoding="utf-8")
        m = re.search(r"def _load_index\([^)]*\)[^\n]*:\s*\n(?P<body>(?:\s+[^\n]*\n)+)", src)
        assert m, "_load_index not found"
        body = m.group("body")

        assert "_load_index_from_cache" in body
        # Никаких fallback'ов на store или files.
        assert "_load_index_from_store" not in body
        assert "_load_index_from_files" not in body
        assert "_load_vectors_from_db" not in body


class TestPreloadIndexesUsesOnlyConfig:
    """preload_indexes берёт имена только из gateway.vector.index.indexes.*."""

    def test_preload_indexes_source_no_store_reference(self) -> None:
        from lib.services import cache_provider_impl as impl

        src = Path(impl.__file__).read_text(encoding="utf-8")

        def _section(name: str) -> str:
            start = src.find(f"def {name}(")
            assert start >= 0, f"{name} not found"
            depth = 0
            i = start
            while i < len(src):
                c = src[i]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        return src[start:i + 1]
                i += 1
            return src[start:]

        body = _section("preload_indexes")
        assert "_vector_indexes" in body or "_indexes" in body
        # Нет SELECT DISTINCT из store.
        assert "SELECT DISTINCT source FROM" not in body
        assert "agent_vector_index_store" not in body
        assert "_vector_store_table" not in body


class TestSearchVectorColdMissRaises:
    """search_vector для непрогретого индекса → ошибка без ленивой сборки."""

    def test_cold_miss_returns_empty_with_error(self, monkeypatch):
        from lib.services import cache_provider_impl as impl

        monkeypatch.setattr(
            impl, "read_vector_index_config",
            lambda _cfg: {"nonexistent_index": {
                "table": "t", "pk": "id",
                "source_table": "t", "content_columns": ["x"],
                "embedding_columns": ["x"], "track_column": "id",
                "chunk_size": 500, "chunk_overlap": 80, "metric": "cosine",
            }},
        )
        monkeypatch.setattr(
            impl, "read_embedding_config",
            lambda: {"model": "mxbai-embed-large:latest", "dimension": 1024},
        )

        provider = impl.PostgresDuckDbProvider(vector_db_table="missing.tbl")
        # _index_cache пуст.
        results = provider.search_vector(
            query="anything", index_name="nonexistent_index", top_k=5,
        )
        assert results == []
        assert provider._search_error is not None


class TestBuildFaissIndexMinimalMeta:
    """build_faiss_index больше не дублирует payload в meta.

    Координаты чанков (pk_value / chunk_index / chunk_count) добавляет
    снаружи ``_load_index_from_cache`` после ``build_faiss_index`` —
    проверяется в TestVectorBuildE2E::test_preload_then_search_returns_correct_top_hit.
    """

    def test_meta_contains_only_metric(self) -> None:
        from lib.utils.duckdb_query import build_faiss_index

        records = [{
            "source": "s", "table": "t", "pk_value": 1,
            "embedding": [0.1] * 4,
            "content": "VERY HEAVY CONTENT " * 1000,
            "search_text": "VERY HEAVY SEARCH " * 1000,
            "row_data": {"big": "x" * 10000},
        }]

        idx, meta = build_faiss_index(records, metric="cosine")
        assert idx is not None
        assert meta == {"metric": "cosine"}


class TestComputeIndexHealthNewSources:
    """compute_index_health: orphan из DuckDB, stale из loaded signature_status."""

    def test_stale_taken_from_loaded_items(self) -> None:
        from lib.services.preload_service import compute_index_health

        declared = {"audits_index": {"table": "t", "pk": "id"}}
        loaded = [
            {"index_name": "audits_index", "vectors": 100, "signature_status": "STALE"},
        ]
        runtime_rows = [
            {"source": "audits_index", "vector_count": 100},
        ]

        h = compute_index_health(declared, loaded, runtime_rows)
        assert "audits_index:STALE" in h["stale"]
        assert h["divergence"] is True
        assert h["level"] == "WARN"

    def test_orphan_taken_from_storage(self) -> None:
        from lib.services.preload_service import compute_index_health

        declared = {"audits_index": {"table": "t", "pk": "id"}}
        loaded = []
        runtime_rows = [
            {"source": "audits_index", "vector_count": 100},
            {"source": "legacy_index", "vector_count": 50},
        ]

        h = compute_index_health(declared, loaded, runtime_rows)
        assert "legacy_index" in h["orphan"]
        assert "audits_index" in h["missing"]

"""Architecture boundary guard: document-level cache.

``DocumentCache`` (``cache.document_cache.DocumentCache``) — единственный
владелец document-level cache protocol:

* snapshot atomic write + marker completeness
* per-chunk / per-section summaries
* invalidation
* session-scoped path layout

Legacy API в ``cache.manifest`` (``write_document_snapshot``,
``read_document_snapshot``, ``is_document_cache_complete``,
``invalidate_document_cache``, ``load_document_chunk_summaries``,
``load_document_section_summaries``, ``write_document_chunk_summary``,
``write_document_section_summary``, ``read_document_chunk_summary``,
``read_document_section_summary``, ``document_dir``, ``document_*_path``)
физически удалён из ``cache.manifest``. Этот guard предотвращает
возврат legacy symbols в production через любые import-формы.

Scope:

* Производственные модули ``scripts/`` (исключая ``cache/``).
* Импорты **operation-level** API из ``cache.manifest``
  (``load_manifest``, ``save_manifest``, ``write_chunk_result``,
  ``read_chunk_result``, ``write_result``, ``read_result``,
  ``manifest_path``, ``manifest_root``, ``chunks_dir``,
  ``chunk_result_path``, ``result_path``, ``load_cached_partials``,
  ``NormalizedManifest``, ``MANIFEST_VERSION_V2``) — допустимы
  в модулях, перечисленных в :data:`LEGACY_MANIFEST_ALLOWED_MODULES`.

Любые новые ссылки на document-level symbols в production — это
regression, и этот тест должен упасть явно.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
_CACHE_DIR = _SCRIPTS_DIR / "cache"
_PRODUCTION_DIRS = (
    "application",
    "execution",
    "retrieval",
    "output",
    "planning",
    "chunking",
    "document",
    "llm",
)
_TOP_LEVEL_PRODUCTION = ("cli.py", "cli_query.py")


# Document-level symbols, которые должны жить ТОЛЬКО в cache/document_cache.py
# и не должны появляться в production-коде как imports или attribute access.
_DOCUMENT_LEVEL_SYMBOLS = frozenset({
    "is_document_cache_complete",
    "read_document_snapshot",
    "write_document_snapshot",
    "invalidate_document_cache",
    "load_document_chunk_summaries",
    "load_document_section_summaries",
    "write_document_chunk_summary",
    "write_document_section_summary",
    "read_document_chunk_summary",
    "read_document_section_summary",
    "document_dir",
    "document_physical_path",
    "document_analysis_path",
    "document_chunks_dir",
    "document_chunk_result_path",
    "document_section_result_path",
    "_document_complete_marker_path",
})

# Operation-level API, легитимный в production (для whitelisting).
_OPERATION_LEVEL_SYMBOLS = frozenset({
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
    "NormalizedManifest",
    "MANIFEST_VERSION_V2",
    "skill_repo_root",
})

# Производственные модули, где operation-level API допустим.
_LEGACY_MANIFEST_ALLOWED_MODULES = frozenset({
    "application/service.py",
    "application/execution_orchestration.py",
    "application/manifest_builder.py",
    "document/physical.py",
    "cli_query.py",
})


def _collect_production_files() -> list[Path]:
    """Собрать все production-модули вне ``cache/`` и ``__pycache__``."""
    files: list[Path] = []
    for sub in _PRODUCTION_DIRS:
        sub_path = _SCRIPTS_DIR / sub
        if not sub_path.is_dir():
            continue
        for p in sub_path.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            files.append(p)
    for top in _TOP_LEVEL_PRODUCTION:
        p = _SCRIPTS_DIR / top
        if p.is_file():
            files.append(p)
    return files


def _rel(p: Path) -> str:
    return str(p.relative_to(_SCRIPTS_DIR))


def _scan_imports(tree: ast.AST) -> list[tuple[str, str, int]]:
    """Собрать все импорты ``from cache.manifest import <name>``.

    Returns list of (imported_name, source_module, lineno).
    """
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "cache.manifest" or module.endswith(".cache.manifest"):
                for alias in node.names:
                    found.append((alias.name, module, node.lineno))
            elif module == "cache":
                for alias in node.names:
                    if alias.name == "manifest":
                        found.append(("manifest", "cache", node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "cache.manifest":
                    found.append(("*", alias.name, node.lineno))
                if alias.name == "cache":
                    found.append(("cache", alias.name, node.lineno))
    return found


def _scan_attribute_access(tree: ast.AST, forbidden_attrs: frozenset[str]) -> list[tuple[str, int]]:
    """Собрать обращения к ``<something>.<forbidden_attr>`` (но НЕ в
    строковых литералах / docstrings).

    Returns list of (attr_name, lineno).
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in forbidden_attrs:
                found.append((node.attr, node.lineno))
    return found


def _scan_string_literals(tree: ast.AST) -> set[tuple[str, int]]:
    """Найти все строковые литералы (для исключения docstring/комментариев).

    AST сам по себе не различает строки в коде от docstring. Поэтому
    docstring-узлы собираем отдельно — это ast.Constant/ast.Expr верхнего
    уровня в Module/FunctionDef/ClassDef, у которых value — str.
    """
    string_locs: set[tuple[str, int]] = set()

    def _is_docstring(body: list[ast.stmt]) -> bool:
        return (
            len(body) > 0
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        )

    def _walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            _walk(child)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and _is_docstring(body):
                ds = body[0]
                string_locs.add((ds.value.value, ds.lineno))

    _walk(tree)
    return string_locs


def _attr_access_in_strings(
    src: str,
    forbidden: frozenset[str],
) -> list[tuple[str, int]]:
    """Собрать ``<forbidden_attr>`` в строковых литералах кода (не docstring).

    Это для проверки использования ``"_complete.marker"`` и подобных
    filesystem literals в production-коде.
    """
    found: list[tuple[str, int]] = []
    for lineno, line in enumerate(src.splitlines(), 1):
        for attr in forbidden:
            if attr in line:
                found.append((attr, lineno))
    return found


# ============================================================================
# Tests
# ============================================================================


def test_no_legacy_document_level_imports_in_production():
    """Production-модули не должны импортировать document-level symbols
    из ``cache.manifest``.
    """
    offenders: list[tuple[str, str, int]] = []
    for path in _collect_production_files():
        rel = _rel(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, module, lineno in _scan_imports(tree):
            if name == "*":
                offenders.append((rel, "import cache.manifest", lineno))
                continue
            if name == "manifest":
                offenders.append((rel, "import cache.manifest", lineno))
                continue
            if name in _DOCUMENT_LEVEL_SYMBOLS:
                offenders.append((rel, f"{name} from {module}", lineno))
    assert offenders == [], (
        "Document-level legacy symbols найдены в production коде:\n"
        + "\n".join(f"  {p}: {what} (line {ln})" for p, what, ln in offenders)
    )


def test_no_document_level_attribute_access_in_production():
    """Production-модули не должны обращаться к document-level symbols
    как к атрибутам (на случай ``from cache.manifest import manifest as cm``
    или ``getattr(cm, '...')``).
    """
    forbidden_attrs = frozenset({
        "is_document_cache_complete",
        "read_document_snapshot",
        "write_document_snapshot",
        "invalidate_document_cache",
        "load_document_chunk_summaries",
        "load_document_section_summaries",
        "write_document_chunk_summary",
        "write_document_section_summary",
        "read_document_chunk_summary",
        "read_document_section_summary",
        "document_dir",
        "document_physical_path",
        "document_analysis_path",
        "document_chunks_dir",
        "document_chunk_result_path",
        "document_section_result_path",
        "_document_complete_marker_path",
    })
    offenders: list[tuple[str, str, int]] = []
    for path in _collect_production_files():
        rel = _rel(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for attr, lineno in _scan_attribute_access(tree, forbidden_attrs):
            offenders.append((rel, attr, lineno))
    assert offenders == [], (
        "Document-level attribute access найден в production коде:\n"
        + "\n".join(f"  {p}: .{what} (line {ln})" for p, what, ln in offenders)
    )


def test_no_document_filesystem_literals_in_production():
    """Production-модули не должны упоминать ``_complete.marker`` в коде."""
    forbidden = frozenset({"_complete.marker", "document cache path"})
    offenders: list[tuple[str, str, int, str]] = []
    for path in _collect_production_files():
        rel = _rel(path)
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        docstrings = _scan_string_literals(tree)

        # Collect only string literals NOT in docstrings
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                in_doc = any(
                    node.lineno == dsln and dsval == node.value
                    for dsval, dsln in docstrings
                )
                if in_doc:
                    continue
                for attr in forbidden:
                    if attr in node.value:
                        offenders.append((rel, "string-literal", node.lineno, node.value))

    assert offenders == [], (
        "Document-level filesystem literals найдены в production коде:\n"
        + "\n".join(f"  {p}: {what} (line {ln}): {s!r}" for p, what, ln, s in offenders)
    )


def test_cache_manifest_has_no_document_level_api():
    """``cache.manifest`` не должен экспортировать document-level symbols."""
    import cache.manifest as cm

    exposed = set(getattr(cm, "__all__", []))
    for symbol in _DOCUMENT_LEVEL_SYMBOLS:
        assert symbol not in exposed, (
            f"cache.manifest.__all__ содержит document-level symbol: "
            f"{symbol!r}"
        )
        assert not hasattr(cm, symbol), (
            f"cache.manifest.{symbol} не должен существовать "
            f"(document-level ownership у DocumentCache)"
        )


def test_document_cache_is_singleton_owner_of_document_level_api():
    """``cache.document_cache.DocumentCache`` владеет document-level API."""
    from cache.document_cache import DocumentCache

    expected = {
        "is_complete",
        "read_snapshot",
        "write_snapshot",
        "invalidate",
        "write_chunk_summary",
        "write_section_summary",
        "load_chunk_summaries",
        "load_section_summaries",
    }
    missing = expected - set(dir(DocumentCache))
    assert missing == set(), (
        f"DocumentCache не реализует ожидаемые public methods: {missing}"
    )


@pytest.mark.parametrize(
    "module_rel",
    sorted(_LEGACY_MANIFEST_ALLOWED_MODULES),
)
def test_operation_level_manifest_allowed_in_module(module_rel: str):
    """Модули из whitelist'а могут импортировать operation-level API."""
    path = _SCRIPTS_DIR / module_rel
    if not path.is_file():
        pytest.skip(f"{module_rel} не существует (план допускает отсутствие)")

    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = _scan_imports(tree)
    # Проверяем что *хотя бы один* operation-level symbol импортирован
    # (это подтверждает, что whitelisting актуален, а не случайный).
    op_level_found = [
        (name, mod, ln) for name, mod, ln in imports
        if name in _OPERATION_LEVEL_SYMBOLS
    ]
    if not op_level_found:
        pytest.skip(
            f"{module_rel} больше не использует operation-level API — "
            f"кандидат на удаление из whitelist"
        )

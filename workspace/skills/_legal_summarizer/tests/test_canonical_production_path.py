"""Production-path integration test.

Проверяет, что canonical pipeline действительно используется
при вызове ``summarizer_canonical``. Использует monkeypatch для
отслеживания вызовов canonical-модулей.
"""

from __future__ import annotations

from pathlib import Path

def _write_doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p

def _write_named_doc(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p

def test_canonical_pipeline_used_in_canonical_wrapper(tmp_path: Path, monkeypatch):
    """summarizer_canonical вызывает run_canonical_pipeline."""
    import application.canonical as summarizer_canonical

    call_count = {"n": 0}
    original = summarizer_canonical.run_canonical_pipeline

    def spy(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        summarizer_canonical, "run_canonical_pipeline", spy,
    )

    p = _write_doc(tmp_path, "1. First\n\nContent here.\n\n2. Second\n\nMore.")
    summarizer_canonical.inspect_canonical(
        text="", document_path=p,
    )
    assert call_count["n"] == 1

def test_canonical_pipeline_does_not_import_legacy(monkeypatch):
    """canonical модули не импортируют legacy.

    Проверяем через статический AST-парсинг: для каждого ``from/forbidden``
    import в ``summarizer_canonical`` и ``canonical_retrieval`` тест должен
    упасть. Также проверяется транзитивный import (когда canonical-файл
    импортирует другой canonical-файл, который уже импортирует legacy).
    """
    import ast as _ast
    import application.canonical as summarizer_canonical
    import retrieval.canonical as canonical_retrieval

    forbidden_short = {
        "fingerprint",
        "reducer_strategy",
        "cached_retrieval",
        "document_cache",
        "document_cleanup",
        "sections",
        "tree",
        "brief_strategy",
        "brief_representation",
        "provenance_reconstruction",
        "packing",
        "token_budget",
    }

    def _collect_imports(path: Path) -> set[str]:
        """Вернуть имена (последний компонент импорта), которые файл импортирует."""
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
            tree = _ast.parse(src)
        except (SyntaxError, FileNotFoundError):
            return set()
        names: set[str] = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
        return names

    scripts_root = summarizer_canonical.__file__.rsplit("application", 1)[0]
    for module in (summarizer_canonical, canonical_retrieval):
        module_path = Path(module.__file__ or "")
        # прямые imports
        direct = _collect_imports(module_path)
        forbidden_used = direct & forbidden_short
        assert not forbidden_used, (
            f"{module.__name__} directly imports legacy: {sorted(forbidden_used)}"
        )
        # транзитивные imports: идём только по локальным файлам scripts/
        visited: set[str] = set()
        stack = [module_path]
        transitive_forbidden: set[str] = set()
        while stack and len(visited) < 50:
            current = stack.pop()
            cur_names = _collect_imports(current)
            for short in cur_names & forbidden_short:
                transitive_forbidden.add(f"{current.name}::{short}")
            # follow local relative imports (within scripts/)
            for node in _ast.walk(_ast.parse(current.read_text(encoding="utf-8", errors="replace"))):
                if isinstance(node, _ast.ImportFrom) and node.level and node.module:
                    base = current.parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    target = (base / node.module.replace(".", "/")).resolve()
                    if scripts_root in str(target) and target not in visited:
                        visited.add(target)
                        stack.append(target)
        assert not transitive_forbidden, (
            f"{module.__name__} transitively imports legacy: "
            f"{sorted(transitive_forbidden)}"
        )

def test_canonical_inspection_returns_pipeline_result(tmp_path: Path):
    """inspect_canonical возвращает объект с pipeline_result."""
    from application.canonical import (
        inspect_canonical,
    )

    p = _write_doc(tmp_path, "1. Section\n\nContent.\n\n2. Section\n\nMore.")
    insp = inspect_canonical(text="", document_path=p)
    assert insp.pipeline_result is not None
    assert insp.pipeline_result.analysis is not None
    assert insp.pipeline_result.analysis.identity is not None
    assert insp.pipeline_result.chunks is not None
    assert insp.pipeline_result.validation is not None

def test_canonical_followup_uses_document_analysis(tmp_path: Path, monkeypatch):
    """answer_followup использует DocumentAnalysis, не legacy."""
    import retrieval.canonical as canonical_retrieval

    call_count = {"n": 0}
    original = canonical_retrieval.build_followup_response

    def spy(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(canonical_retrieval, "build_followup_response", spy)

    p = _write_named_doc(
        tmp_path, "doc.txt",
        "1. Общие положения\n\n"
        "Текст о праве собственности.\n\n"
        "2. Обязательства\n\n"
        "Текст о договорных обязательствах.\n\n",
    )

    from application.canonical import (
        build_pipeline_result,
    )
    result = build_pipeline_result(document_path=p)
    canonical_retrieval.answer_followup(
        result.analysis, "что такое право собственности?",
    )
    assert call_count["n"] >= 1
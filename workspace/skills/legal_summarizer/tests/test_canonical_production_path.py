"""Production-path integration test (Этап 28).

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
    from workspace.skills.legal_summarizer.scripts import summarizer_canonical

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
    """canonical модули не импортируют legacy."""
    from workspace.skills.legal_summarizer.scripts import (
        summarizer_canonical, canonical_retrieval,
    )
    import importlib

    forbidden = {
        "workspace.skills.legal_summarizer.scripts.fingerprint",
        "workspace.skills.legal_summarizer.scripts.execution_strategy",
        "workspace.skills.legal_summarizer.scripts.reducer",
        "workspace.skills.legal_summarizer.scripts.reducer_impl",
        "workspace.skills.legal_summarizer.scripts.reducer_strategy",
        "workspace.skills.legal_summarizer.scripts.context_expansion",
        "workspace.skills.legal_summarizer.scripts.cached_retrieval",
        "workspace.skills.legal_summarizer.scripts.document_cache",
        "workspace.skills.legal_summarizer.scripts.document_cleanup",
        "workspace.skills.legal_summarizer.scripts.structure.sections",
        "workspace.skills.legal_summarizer.scripts.structure.tree",
        "workspace.skills.legal_summarizer.scripts.brief_strategy",
        "workspace.skills.legal_summarizer.scripts.brief_representation",
        "workspace.skills.legal_summarizer.scripts.cache_followup",
        "workspace.skills.legal_summarizer.scripts.provenance_reconstruction",
        "workspace.skills.legal_summarizer.scripts.packing",
        "workspace.skills.legal_summarizer.scripts.token_budget",
    }

    for module_name in forbidden:
        try:
            importlib.import_module(module_name)
            in_sys = True
        except ImportError:
            in_sys = False
        if in_sys:
            try:
                spec = importlib.util.find_spec(module_name)
                if spec is None:
                    continue
                mod = importlib.import_module(module_name)
                if hasattr(mod, "__file__") and mod.__file__:
                    pass
            except Exception:
                continue

    for module in (summarizer_canonical, canonical_retrieval):
        module_file = module.__file__ or ""
        for forbidden_mod in forbidden:
            short = forbidden_mod.rsplit(".", 1)[-1]
            assert short not in dir(module), (
                f"{module.__name__} импортирует {short}"
            )


def test_canonical_inspection_returns_pipeline_result(tmp_path: Path):
    """inspect_canonical возвращает объект с pipeline_result."""
    from workspace.skills.legal_summarizer.scripts.summarizer_canonical import (
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
    from workspace.skills.legal_summarizer.scripts import canonical_retrieval

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

    from workspace.skills.legal_summarizer.scripts.summarizer_canonical import (
        build_pipeline_result,
    )
    result = build_pipeline_result(document_path=p)
    canonical_retrieval.answer_followup(
        result.analysis, "что такое право собственности?",
    )
    assert call_count["n"] >= 1
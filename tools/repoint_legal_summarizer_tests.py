"""Repoint legal_summarizer test imports from the legacy
``workspace.skills.legal_summarizer.scripts(.structure)`` layout onto the new
``legal_summarizer.<layer>.<mod>`` package.

Applies deterministic, ordered rewrites to every ``*.py`` under
``workspace/skills/legal_summarizer/tests`` and to every ``*.py`` under
``tests/`` that references the old paths. Safe to re-run (idempotent).

Run from the repo root:
    python tools/repoint_legal_summarizer_tests.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\Алексей\.nanobot")

# old structure module -> new legal_summarizer.<layer>.<mod>
S_MAP: dict[str, str] = {
    "adjacent_packing": "chunking.packing",
    "architecture_guard": "infrastructure.architecture_guard",
    "benchmark": "planning.benchmark",
    "block_lookup": "document.block_lookup",
    "block_ownership": "chunking.block_ownership",
    "brief_budget": "chunking.brief_budget",
    "brief_from_analysis": "application.brief_from_analysis",
    "candidate_aggregator": "retrieval.candidate_aggregator",
    "chunks": "chunking.chunks",
    "context_expansion": "retrieval.context_expansion",
    "document_analysis": "document.analysis",
    "document_chunker": "chunking.chunker",
    "document_loader": "document.loader",
    "execution_plan": "planning.plan",
    "followup": "retrieval.followup",
    "full_doc_fallback": "retrieval.fallback",
    "heading": "document.heading",
    "hierarchy": "document.hierarchy",
    "identity": "domain.identity",
    "importance_brief": "chunking.importance_brief",
    "importance_score": "chunking.importance_score",
    "list_detection": "document.list_detection",
    "models": "domain.models",
    "numbering": "domain.numbering",
    "order": "chunking.order",
    "pdf_outline": "document.pdf_outline",
    "physical": "document.physical",
    "pipeline": "application.pipeline_structure",
    "provenance": "retrieval.provenance",
    "quality_metrics": "retrieval.quality",
    "query_normalizer": "retrieval.normalizer",
    "question_from_analysis": "retrieval.question",
    "reference_qa": "retrieval.qa",
    "repair": "document.repair",
    "retrieval": "retrieval.query",
    "retrieval_index": "retrieval.index",
    "retry": "llm.retry",
    "safety_merge": "document.safety_merge",
    "semantic_record": "retrieval.records",
    "single_flight": "llm.single_flight",
    "title": "document.title",
    "token_estimator": "domain.tokens",
    "unified_execution": "planning.strategy",
    "validation": "document.validation",
}

# old top-level scripts module -> new legal_summarizer.<layer>.<mod>
T_MAP: dict[str, str] = {
    "canonical_retrieval": "retrieval.canonical",
    "legacy_audit": "infrastructure.legacy_audit",
    "llm": "llm.client",
    "llm_calls": "llm.calls",
    "manifest": "cache.manifest",
    "output": "output.presenter",
    "pipeline": "execution.pipeline",
    "prompts": "llm.prompts",
    "prompts_runtime": "llm.prompts_runtime",
    "sanitize": "llm.sanitize",
    "skill_config": "llm.config",
    "summarizer": "application.service",
    "summarizer_canonical": "application.canonical",
}

# flat module name -> new legal_summarizer.<layer>.<mod> (skill tests only)
F_MAP: dict[str, str] = dict(T_MAP)

OLD_PREFIX = "workspace.skills.legal_summarizer.scripts"

SKIP_FILES = {
    # subprocess bootstrap must keep the flat stub shadowing of `summarizer`.
    "tests/test_legal_summarizer_running_subprocess.py",
}


def _iter_files() -> list[Path]:
    files: list[Path] = []
    skill_tests = ROOT / "workspace" / "skills" / "legal_summarizer" / "tests"
    files.extend(skill_tests.rglob("*.py"))
    for p in (ROOT / "tests").rglob("*.py"):
        files.append(p)
    out = []
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        if rel in SKIP_FILES:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        if OLD_PREFIX in text or re.search(
            r"(?m)^\s*(from|import)\s+(summarizer|llm_calls|pipeline|manifest|output|"
            r"sanitize|skill_config|prompts|prompts_runtime|llm|canonical_retrieval|"
            r"summarizer_canonical|legacy_audit)\b",
            text,
        ):
            out.append(f)
    return out


def _transform_skill(text: str) -> tuple[str, bool]:
    changed = False
    out = text

    # 1) dotted structure module path  ->  legal_summarizer.<layer>.<mod>
    for old, new in S_MAP.items():
        pat = re.compile(re.escape(f"{OLD_PREFIX}.structure.") + re.escape(old) + r"(?!\w)")
        out, n = pat.subn("legal_summarizer." + new, out)
        changed |= n > 0

    # 2) dotted top-level module path
    for old, new in T_MAP.items():
        pat = re.compile(re.escape(f"{OLD_PREFIX}.") + re.escape(old) + r"(?!\w)")
        out, n = pat.subn("legal_summarizer." + new, out)
        changed |= n > 0

    # 3) bare  `from ...scripts.structure import <mod>[ as alias]`
    for old, new in sorted(S_MAP.items(), key=lambda kv: -len(kv[0])):
        pat = re.compile(
            r"from\s+" + re.escape(f"{OLD_PREFIX}.structure") + r"\s+import\s+"
            + re.escape(old) + r"(?!\w)(\s+as\s+(\w+))?"
        )

        def _s(m, new=new, old=old):
            alias = m.group(2) or old
            return f"import legal_summarizer.{new} as {alias}"

        out, n = pat.subn(_s, out)
        changed |= n > 0

    # 4) bare  `from ...scripts import <mod>[ as alias]`
    for old, new in sorted(T_MAP.items(), key=lambda kv: -len(kv[0])):
        pat = re.compile(
            r"from\s+" + re.escape(OLD_PREFIX) + r"\s+import\s+"
            + re.escape(old) + r"(?!\w)(\s+as\s+(\w+))?"
        )

        def _t(m, new=new, old=old):
            alias = m.group(2) or old
            return f"import legal_summarizer.{new} as {alias}"

        out, n = pat.subn(_t, out)
        changed |= n > 0

    # 5) flat `import <mod>[ as alias]`
    for nm, new in F_MAP.items():
        pat = re.compile(r"(?m)^(\s*)import " + re.escape(nm) + r"(\s+as\s+(\w+))?\s*$")

        def _i(m, new=new, nm=nm):
            alias = m.group(3) or nm
            return f"{m.group(1)}import legal_summarizer.{new} as {alias}"

        out, n = pat.subn(_i, out)
        changed |= n > 0

    # 6) flat `from <mod> import ...`
    for nm, new in F_MAP.items():
        pat = re.compile(r"(?<!\w)from " + re.escape(nm) + r"\b( import)")
        out, n = pat.subn(f"from legal_summarizer.{new}\\1", out)
        changed |= n > 0

    return out, changed


def main() -> None:
    n_changed = 0
    for f in _iter_files():
        rel = f.relative_to(ROOT).as_posix()
        if rel in SKIP_FILES:
            continue
        text = f.read_text(encoding="utf-8")
        out, changed = _transform_skill(text)
        if changed:
            f.write_text(out, encoding="utf-8")
            n_changed += 1
            print(f"repointed {rel}")
    print(f"\n{ n_changed } test file(s) repointed.")


if __name__ == "__main__":
    sys.exit(main())

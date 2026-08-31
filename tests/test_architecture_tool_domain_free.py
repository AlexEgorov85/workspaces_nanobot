"""Architecture tests: generic tools не содержат domain-specific routing.

Проверяет TARGET_ARCHITECTURE.md §22.3, §22.9.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_IDENTIFIERS = {
    "audit", "violations", "audits_index", "audit_analyzer",
}

# Строковые токены, запрещённые в docstring'ах generic tools.
# Проверяются как подстроки в литералах ``ast.Constant`` внутри docstring.
# ``audit`` как самостоятельное слово здесь НЕ включено намеренно — имя
# ``tool_audit_hook`` (инфра) легитимно; проверка идёт по уникальным
# домен-маркерам (``oarb``, именам таблиц/индексов конкретного домена).
FORBIDDEN_STRING_TOKENS = (
    "oarb",
    "audit_analyzer",
    "audits_index",
    "violations_index",
    "audit_reports_index",
    "audit_reports",
    "auditee_entity",
)


def _tool_files() -> list[Path]:
    tools_dir = REPO_ROOT / "workspace" / "tools"
    return [p for p in tools_dir.glob("*.py") if p.name != "__init__.py"]


def _collect_identifiers(tree: ast.AST) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name:
                out.append((node.name, node.lineno))
        elif isinstance(node, ast.Name):
            out.append((node.id, node.lineno))
        elif isinstance(node, ast.Attribute):
            out.append((node.attr, node.lineno))
        elif isinstance(node, ast.arg):
            if node.arg:
                out.append((node.arg, node.lineno))
    return out


def _violates_forbidden(identifiers: list[tuple[str, int]]) -> list[tuple[str, int]]:
    return [
        (name, lineno) for name, lineno in identifiers
        if name in FORBIDDEN_IDENTIFIERS
    ]


class TestToolDescriptionsHaveNoDomain:
    """Tool.description не должна содержать audit-домен (TARGET §23)."""

    @pytest.mark.parametrize(
        "tool_path",
        [str(p.relative_to(REPO_ROOT)) for p in _tool_files()],
    )
    def test_description_no_audit(self, tool_path: str) -> None:
        source = (REPO_ROOT / tool_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        docstrings: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                docstring = ast.get_docstring(node)
                if docstring:
                    docstrings.append(docstring)
        # Только description'ы tool-классов (начинаются с имени класса и идут в начало docstring)
        for doc in docstrings:
            lower = doc.lower()
            if "execute a read-only sql" in lower or "search a configured vector index" in lower:
                # Это явные description — проверяем
                for word in ("audit", "violations", "audits_index"):
                    assert word not in lower, (
                        f"{tool_path} description contains domain word '{word}':\n{doc[:200]}"
                    )


class TestToolCodeNoDomainIdentifiers:
    """Tool code не должен содержать audit-домен в именах (TARGET §22.3)."""

    @pytest.mark.parametrize(
        "tool_path",
        [str(p.relative_to(REPO_ROOT)) for p in _tool_files()],
    )
    def test_no_audit_identifiers(self, tool_path: str) -> None:
        source = (REPO_ROOT / tool_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = _violates_forbidden(_collect_identifiers(tree))
        assert not offenders, (
            f"{tool_path} contains forbidden domain identifiers: {offenders}. "
            "Tool code must be domain-free (TARGET §22.3)."
        )


class TestToolNoDomainRouting:
    """Tool не должен содержать domain routing (TARGET §22.9)."""

    @pytest.mark.parametrize(
        "tool_path",
        [str(p.relative_to(REPO_ROOT)) for p in _tool_files()],
    )
    def test_no_caller_or_skill_routing(self, tool_path: str) -> None:
        source = (REPO_ROOT / tool_path).read_text(encoding="utf-8")
        forbidden_patterns = [
            'if caller ==',
            'if skill ==',
            'if domain ==',
            'if caller in',
            'if skill in',
        ]
        for pattern in forbidden_patterns:
            assert pattern not in source, (
                f"{tool_path} contains forbidden routing pattern '{pattern}'. "
                "Generic tool must not contain caller/skill/domain routing (TARGET §22.9)."
            )


def _iter_tool_docstring_constants(tree: ast.AST) -> list[tuple[str, int]]:
    """Собрать все строковые литералы, привязанные к docstring'ам tool'ов.

    Docstring в Python представлен как первый стейтмент функции/класса с
    ``ast.Constant`` ``str``-значением. Возвращает пары (значение, line_no).
    """
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            out.append((first.value.value, first.value.lineno))
    return out


class TestToolDocstringsNoDomainLiterals:
    """String literals в docstring'ах generic tools не должны содержать
    domain-маркеры (TARGET §22.3).

    AST-проверка ``FORBIDDEN_IDENTIFIERS`` ловит только имена
    (FunctionDef/Name/arg/Attribute) — этого недостаточно: docstring
    ``"не знает про oarb.*"`` спокойно проходит. Этот тест покрывает
    именно строковые литералы внутри docstring'ов, где leak и наблюдался.
    """

    @pytest.mark.parametrize(
        "tool_path",
        [str(p.relative_to(REPO_ROOT)) for p in _tool_files()],
    )
    def test_no_domain_tokens_in_docstrings(self, tool_path: str) -> None:
        source = (REPO_ROOT / tool_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders: list[tuple[str, int, str]] = []
        for doc_value, doc_line in _iter_tool_docstring_constants(tree):
            lower = doc_value.lower()
            for token in FORBIDDEN_STRING_TOKENS:
                if token.lower() in lower:
                    offenders.append((token, doc_line, doc_value[:200]))
        assert not offenders, (
            f"{tool_path} docstrings contain forbidden domain tokens: "
            f"{[(t, l) for t, l, _ in offenders]}. "
            "Tool docstrings must be domain-free (TARGET §22.3). "
            "Generic infrastructure layer should not reference concrete "
            "tables/indexes from any skill."
        )
"""Тесты архитектурных guard'ов change ``fix-history-search-user-isolation``.

Primary security check — тесты ``TestHistorySearchGeneratedSqlGuard``
(сгенерированный SQL и параметры) в ``test_history_search_tool.py``.
Этот файл — supplementary grep-guard по исходнику
``workspace/tools/history_search_tool.py``: страховка от случайного
возврата unscoped-формы после рефакторинга, не primary check.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parent.parent
_HISTORY_SEARCH_TOOL = _REPO / "workspace" / "tools" / "history_search_tool.py"


class TestHistorySearchSourceGuard:
    """Supplementary guard: в исходнике ``history_search_tool.py`` нет
    запрещённых паттернов unscoped-fallback'а. Это страховка от регрессии
    после рефакторинга, не primary security check (он — в
    ``TestHistorySearchGeneratedSqlGuard`` через mock fetch)."""

    def _strip_comments(self, src: str) -> str:
        """Убрать комментарии и docstring, чтобы guard не ругался на текст в них."""
        tree = ast.parse(src)
        lines = src.splitlines(keepends=True)

        # Соберём множество (start, end) line ranges для docstring'ов.
        string_ranges: list[tuple[int, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr):
                    value = body[0].value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        start = getattr(body[0], "lineno", None) or getattr(
                            node, "lineno", 1
                        )
                        end = body[0].end_lineno or start
                        string_ranges.append((start, end))

        # Модульный docstring — отдельная ветка.
        body = getattr(tree, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            string_ranges.append((1, body[0].end_lineno or 1))

        # Соберём строки-комментарии (line numbers, начинающиеся с #).
        comment_lines: set[int] = set()
        for i, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                comment_lines.add(i)

        out_lines: list[str] = []
        for i, line in enumerate(lines, 1):
            in_doc = any(start <= i <= end for start, end in string_ranges)
            if in_doc or i in comment_lines:
                out_lines.append("\n")  # заменить на пустую строку (line numbers сохранятся)
            else:
                out_lines.append(line)
        return "".join(out_lines)

    def _read_source(self) -> str:
        return _HISTORY_SEARCH_TOOL.read_text(encoding="utf-8")

    def _read_code_only(self) -> str:
        return self._strip_comments(self._read_source())

    def test_no_or_session_id_fallback_pattern(self):
        """В коде НЕТ ``(%s OR session_id = %s)`` — старый unscoped fallback,
        при котором ``session_scope='all'`` возвращал глобальный набор событий."""
        src = self._read_code_only()
        assert "(%s OR session_id = %s)" not in src, (
            "Запрещённый unscoped-fallback в history_search_tool.py"
        )

    def test_no_where_true_or_or_true(self):
        """В коде НЕТ ``WHERE TRUE`` / ``OR TRUE`` — запрещённые
        паттерны unscoped-выборки."""
        src = self._read_code_only().upper()
        assert "WHERE TRUE" not in src
        assert "OR TRUE" not in src

    def test_no_session_id_like_pattern(self):
        """В коде НЕТ ``session_id LIKE``."""
        src = self._read_code_only()
        assert "session_id LIKE" not in src

    def test_no_is_null_or_user_id_pattern(self):
        """В коде НЕТ ``IS NULL OR user_id`` — запрещённый unscoped-fallback."""
        src = self._read_code_only()
        assert "IS NULL OR user_id" not in src

    def test_no_sender_id_access_outside_helper(self):
        """``sender_id`` в исходнике — ТОЛЬКО внутри функции
        ``_current_user_id()`` (инкапсуляция зависимости от nanobot 0.3.0).
        Иначе — кто угодно может начать резолвить чужой identity из
        произвольного места.

        Реализация: используем ``ast.NodeVisitor`` с трекингом текущей
        функции через ``ast.walk`` + ``ast.FunctionDef.scope`` (Python 3.14).
        """
        src = self._read_code_only()
        tree = ast.parse(src)

        # Соберём для каждого ``Attribute(attr='sender_id')`` список
        # функций, в которых он лежит (через вложенный обход).
        helper_defined = False
        sender_id_attrs: list[ast.Attribute] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_current_user_id":
                helper_defined = True
                for child in ast.walk(node):
                    if isinstance(child, ast.Attribute) and child.attr == "sender_id":
                        sender_id_attrs.append(child)
                break

        assert helper_defined, "_current_user_id() helper должен быть в файле"
        # Все ``Attribute(attr='sender_id')`` найденные ОБХОДОМ ВСЕГО файла
        # должны быть subset'ом того, что найдено внутри ``_current_user_id``.
        all_sender_id_attrs = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "sender_id"
        ]
        helper_attrs_set = {(a.lineno, a.col_offset) for a in sender_id_attrs}
        outside_attrs = [
            (a.lineno, a.col_offset)
            for a in all_sender_id_attrs
            if (a.lineno, a.col_offset) not in helper_attrs_set
        ]
        assert not outside_attrs, (
            f"sender_id должен быть ТОЛЬКО в _current_user_id(), "
            f"но встретился в: {outside_attrs[:5]}"
        )

    def test_no_get_request_user_id_call_outside_db_logging(self):
        """У ``DbLoggingService`` НЕТ публичного ``get_request_user_id``
        (или эквивалента). Тест на исходник сервиса — дополнительная
        страховка (основная — в test_db_logging_service.py)."""
        svc_src = (_REPO / "lib" / "services" / "db_logging_service.py").read_text(
            encoding="utf-8"
        )
        forbidden = (
            "def get_request_user_id",
            "def lookup_user_id",
            "def resolve_user_id",
        )
        for pat in forbidden:
            assert pat not in svc_src, (
                f"DbLoggingService не должен иметь публичный метод: {pat}"
            )

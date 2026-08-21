"""Статический аудит исходника ``postgres_channel.py``.

Проверяет на уровне Python-source, что:
  1. Каждый f-string SQL с подстановкой ``self._fq_claims`` находится
     внутри метода, начинающегося с гарда
     ``if self._claim_strategy != "worker_pool": return ...``.
  2. Все обращения к ``_fq_claims`` в SQL — только в worker_pool-ветках
     или в гардированных методах.
  3. ``_claim_one`` маршрутизирует single-режим в ``_claim_one_single``
     через ``if self._claim_strategy == "single": return await self._claim_one_single()``.
  4. ``_delete_claim`` имеет гард на ``worker_pool``.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_PG_PATH = (
    Path(__file__).resolve().parent.parent
    / "lib"
    / "channels"
    / "postgres_channel.py"
)


def _parse_pg():
    return ast.parse(_PG_PATH.read_text(encoding="utf-8"))


def _method_by_name(tree, name: str) -> ast.AsyncFunctionDef | None:
    """Найти async-функцию по имени в классе PostgresChannel."""
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "PostgresChannel"
    )
    for node in cls.body:
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == name
        ):
            return node
    return None


def _fq_claims_in_sql_strings(method: ast.AsyncFunctionDef) -> list[int]:
    """Найти номера строк всех f-strings, содержащих ``_fq_claims``."""
    lines: list[int] = []
    for node in ast.walk(method):
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    src = ast.unparse(value.value)
                    if "fq_claims" in src:
                        lines.append(node.lineno)
                        break
    return lines


def _has_guard(method: ast.AsyncFunctionDef, guard_keyword: str) -> bool:
    """Проверить, что метод начинается (после docstring) с ``if ...: return``.

    Ищем первый ``ast.If`` statement в теле метода, в котором test
    содержит ключевое слово ``guard_keyword``. Ищем без учёта кавычек.
    """
    # Нормализуем искомую фразу: убираем варианты кавычек
    # ищем просто claim_strategy != worker_pool
    norm = guard_keyword.replace("'", "").replace('"', "")
    for stmt in method.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # docstring
        if isinstance(stmt, ast.If):
            test_src = ast.unparse(stmt.test).replace("'", "").replace('"', "")
            if norm in test_src:
                return any(
                    isinstance(s, ast.Return) for s in stmt.body
                )
        return False  # первый statement — не if с гардом
    return False


def test_lease_loop_has_worker_pool_guard():
    """``_lease_loop`` гардирован ``claim_strategy != 'worker_pool'``."""
    tree = _parse_pg()
    method = _method_by_name(tree, "_lease_loop")
    assert method is not None, "_lease_loop не найден"
    # В исходнике используются одинарные кавычки 'worker_pool'
    assert _has_guard(method, "claim_strategy != 'worker_pool'"), (
        "_lease_loop должен начинаться с гарда worker_pool"
    )


def test_reclaim_needed_has_worker_pool_guard():
    """``_reclaim_needed`` гардирован."""
    tree = _parse_pg()
    method = _method_by_name(tree, "_reclaim_needed")
    assert method is not None
    assert _has_guard(method, "claim_strategy != 'worker_pool'"), (
        "_reclaim_needed должен гардироваться"
    )


def test_reclaim_and_heal_has_worker_pool_guard():
    """``_reclaim_and_heal`` гардирован."""
    tree = _parse_pg()
    method = _method_by_name(tree, "_reclaim_and_heal")
    assert method is not None
    assert _has_guard(method, "claim_strategy != 'worker_pool'"), (
        "_reclaim_and_heal должен гардироваться"
    )


def test_delete_claim_has_worker_pool_guard():
    """``_delete_claim`` гардирован — в single no-op."""
    tree = _parse_pg()
    method = _method_by_name(tree, "_delete_claim")
    assert method is not None
    assert _has_guard(method, "claim_strategy != 'worker_pool'"), (
        "_delete_claim должен иметь гард claim_strategy != worker_pool"
    )


def test_claim_one_routes_single_to_single_method():
    """``_claim_one`` маршрутизирует single в ``_claim_one_single``."""
    tree = _parse_pg()
    method = _method_by_name(tree, "_claim_one")
    assert method is not None
    src = ast.unparse(method)
    # Ищем оба варианта кавычек
    assert (
        '_claim_strategy == "single"' in src
        or "_claim_strategy == 'single'" in src
    ), (
        "_claim_one должен делегировать в _claim_one_single для single-режима"
    )
    assert "self._claim_one_single()" in src, (
        "_claim_one должен вызывать self._claim_one_single()"
    )


def test_claim_one_single_method_exists():
    """``_claim_one_single`` определён (single-путь)."""
    tree = _parse_pg()
    method = _method_by_name(tree, "_claim_one_single")
    assert method is not None, "_claim_one_single не определён"
    # Не должен содержать _fq_claims (т.е. SQL к claims)
    lines_with_claims = _fq_claims_in_sql_strings(method)
    assert lines_with_claims == [], (
        f"_claim_one_single не должен содержать SQL с _fq_claims, "
        f"но нашёл на строках: {lines_with_claims}"
    )


def test_lease_methods_have_no_sql_outside_guard():
    """Sanity-check: ``_lease_loop``, ``_reclaim_needed`` и ``_reclaim_and_heal``
    содержат SQL с _fq_claims ТОЛЬКО ПОСЛЕ гарда (т.е. в worker_pool-ветке).

    Это проверка того, что гард стоит ДО SQL, а не после.
    """
    for method_name in ("_lease_loop", "_reclaim_needed", "_reclaim_and_heal"):
        tree = _parse_pg()
        method = _method_by_name(tree, method_name)
        assert method is not None

        # Найти позицию guard'а (первый ast.If с claim_strategy) и первого SQL с _fq_claims
        guard_line = None
        claims_line = None
        for stmt in method.body:
            if isinstance(stmt, ast.If):
                src = ast.unparse(stmt.test)
                if (
                    "claim_strategy" in src
                    and "worker_pool" in src
                    and "!=" in src
                ):
                    guard_line = stmt.lineno
                    break

        for node in ast.walk(method):
            if isinstance(node, ast.JoinedStr):
                for value in node.values:
                    if isinstance(value, ast.FormattedValue):
                        if "fq_claims" in ast.unparse(value.value):
                            claims_line = node.lineno
                            break
                if claims_line:
                    break

        assert guard_line is not None, (
            f"{method_name}: не найден гард worker_pool"
        )
        assert claims_line is not None, (
            f"{method_name}: SQL с _fq_claims не найден (странно для sanity-проверки)"
        )
        assert guard_line < claims_line, (
            f"{method_name}: гард на строке {guard_line} должен быть ДО "
            f"SQL на строке {claims_line}"
        )


def test_no_other_method_emits_claims_sql():
    """Sanity-check: никакой другой метод PostgresChannel не имеет
    f-string SQL с ``_fq_claims`` — это ловит регрессии, если кто-то
    добавит новый метод с прямым обращением к claims.
    """
    tree = _parse_pg()
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "PostgresChannel"
    )

    methods_with_claims = set()
    for node in cls.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("_"):
            continue
        lines = _fq_claims_in_sql_strings(node)
        if lines:
            methods_with_claims.add(node.name)

    # Эти методы МОГУТ содержать _fq_claims (они worker_pool-специфичные
    # или само ядро single/worker_pool):
    allowed = {
        "_claim_one",        # worker_pool ветка
        "_claim_one_single", # single ветка (НЕ должно содержать _fq_claims)
        "_lease_loop",
        "_reclaim_needed",
        "_reclaim_and_heal",
        "_delete_claim",
    }

    not_allowed = methods_with_claims - allowed
    assert not not_allowed, (
        f"Методы с прямым SQL к _fq_claims, не входящие в allowed: {not_allowed}. "
        f"Если это новый метод, добавьте явный гард claim_strategy."
    )
